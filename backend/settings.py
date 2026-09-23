from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlparse


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

# 批量制作成品目录；每个抖音链接最终整理为一个独立发布文件夹。
BATCH_OUTPUT_ROOT = Path(
    os.getenv("H3_BATCH_OUTPUT", r"E:\AI_Exports\H3-MotionStudio\发布成品")
)
# 发布根下的**类型子目录**（2026-09-20 用户：「在发布目录里加一个跳舞文件夹 一个唱歌文件夹
# 各自放对应的内容」→「子目录名就叫可以叫英文」）：唱歌 → `singing/`、跳舞 → `dance/`。
# 根目录本身仍然保留 `4比3图片.txt` / `9比16图片.txt` / `晚柠.png`（手工出图脚本在读，见
# `scripts/make_publish_assets_via_cdp.py`），所以只**加一层**、不搬任何东西。
# ⚠️ 子目录名只用英文：中文名会让剪映/ffmpeg 那几条链路的路径处理更容易出编码问题。
PUBLISH_KIND_DIRS = {"singing": "singing", "dance": "dance"}


def publish_subdir(kind: str) -> str:
    """某类型作品在发布根下的子目录名；类型未知返回空串（= 直接用发布根，历史条目兜底）。

    只在**新建目录**与**扫描目录**时用；老目录（在扁平发布根里）一律靠调用方的
    "子目录优先、扁平根兜底"逻辑复用，绝不改名/搬移（用户 2026-09-20：「老项目不要用管」）。
    """
    return PUBLISH_KIND_DIRS.get(str(kind or "").strip(), "")
# **默认端口都挪到 15000 以上**（用户 2026-09-19：「直接帮我改掉 不要用这些端口了 改成系统不要的」）。
# 原因：本机 TCP 动态端口范围被改成 1024~15000，Hyper-V/WSL 的 NAT 会在这段里按 100 个一组
# 预留端口，8111/8188 正好被圈进去（uvicorn/comfyui 报 winerror 10013 —— 是"拒绝访问"，
# 不是"已被占用"），而且**每次开机重新分配**。15000 以上不在动态范围，系统不会预留。
BATCH_SELF_URL = os.getenv("H3_SELF_URL", "http://127.0.0.1:15111").rstrip("/")

COMFY_HOME = Path(os.getenv("H3_COMFY_HOME", r"D:\Comfyui")).resolve()
COMFY_ROOT = COMFY_HOME / "ComfyUI"
COMFY_INPUT = COMFY_ROOT / "input"
COMFY_OUTPUT = COMFY_ROOT / "output"
COMFY_PYTHON = COMFY_HOME / "python_embeded" / "python.exe"
COMFY_MAIN = COMFY_ROOT / "main.py"
COMFY_URL = os.getenv("H3_COMFY_URL", "http://127.0.0.1:15188").rstrip("/")
COMFY_WS = COMFY_URL.replace("http://", "ws://").replace("https://", "wss://")
# ComfyUI 的**监听端口与 URL 同源**，不要再在 pipeline 里另写死端口：
# 启动脚本会把端口选好并通过 H3_COMFY_URL 交给后端，pipeline 拉起的那份必须跟着一起换，
# 否则「后端起在 15188、ComfyUI 却监听别的端口」，健康检查永远不过。
COMFY_PORT = urlparse(COMFY_URL).port or 15188

# Windows 用户级环境变量的一次性缓存：后端常由 .bat / 计划任务 / npm 启动，
# 这些方式不继承用户级变量，不补读会让凭据配置静默失效。
_USER_ENV_CACHE: dict[str, str] = {}

# **用户级注册表优先**的几个开关：进程环境里的值可能是**上一次会话的残留**
# （2026-09-16 实测：用户把 H3_AUTO_CHATGPT_IMAGE/COVER 改成 0 之后重启后端，
#  仍然是「自动出图开」——因为后端是 DSH/终端拉起的，继承了它们启动时的旧环境变量，
#  `os.environ` 里还是 1，把用户刚改的 0 盖掉了）。
# 这几个开关的语义是「用户现在要不要图片自动化」，所以**以注册表为准**。
_REGISTRY_FIRST_KEYS = {
    "H3_AUTO_CHATGPT_IMAGE",
    "H3_AUTO_CHATGPT_COVER",
    "H3_AUTO_CONFIRM",
    "H3_FULL_AUTO",
}


def full_auto_enabled() -> bool:
    """**全自动链路**（用户 2026-09-19：「全自动吧。不要我去备料了。现在直接用api生成」）。

    打开时批量备料自己把三张图都出了、并自动放行出片，用户只需要贴链接：

      · 人物图：备料阶段直接用中转站出（糊脸 + 身份图 + 相似度 <70 自动重做）；
      · 两张封面：人物图与发布文案落进发布目录之后，同样走中转站出（与出片并行）；
      · 自动放行：出完图直接确认出片，不再停在审核点等人点。

    `H3_FULL_AUTO=0` 可以整体退回「只备料、图由用户自己出、审核点手动确认」的老流程。
    """
    raw = str(env_value("H3_FULL_AUTO", "1") or "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def env_value(name: str, default: str = "") -> str:
    """读环境变量：**用户级注册表优先**的那几个开关先看注册表，其余进程环境优先。

    凭据始终只来自环境，不写入前端、数据库或仓库。
    """
    if os.name == "nt" and name in _REGISTRY_FIRST_KEYS:
        registry = _user_env(name)
        if registry:
            return registry
    value = (os.environ.get(name) or "").strip()
    if value:
        return value
    return _user_env(name) or default


def _user_env(name: str) -> str:
    """Windows 用户级环境变量（带一次性缓存）。"""
    if os.name != "nt":
        return ""
    if name not in _USER_ENV_CACHE:
        _USER_ENV_CACHE[name] = ""
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as handle:
                _USER_ENV_CACHE[name] = str(winreg.QueryValueEx(handle, name)[0] or "").strip()
        except OSError:
            pass
    return _USER_ENV_CACHE[name]


WORKFLOW_DIR = COMFY_ROOT / "user" / "default" / "workflows" / "video"
SINGING_WORKFLOW = WORKFLOW_DIR / "视频-单图唱歌-自动拼接40秒内-4x3-运镜版.json"
UPSCALE_WORKFLOW = WORKFLOW_DIR / "视频-成片输入-独立二采-RealESRGAN4x转1080P-8GB高清加强版.json"
# VOSR2 版二采：与 RealESRGAN 版**接口节点 id 完全一致**（2 输入视频 / 5 目标尺寸 /
# 7 帧率 / 8 输出），所以 `prepare_upscale_workflow()` 不用改。`H3_UPSCALE_ENGINE=vosr2`
# 时启用；默认 esrgan，回滚只改这一个词。
UPSCALE_WORKFLOW_VOSR2 = WORKFLOW_DIR / "视频-成片输入-独立二采-VOSR2-转1080P-8GB.json"
# 动作迁移链路：先去字幕（ProPainter 固定底部），再长视频替换/动作迁移
CLEAN_WORKFLOW = WORKFLOW_DIR / "视频-去字幕-ProPainter-固定底部.json"
MIGRATE_WORKFLOW = WORKFLOW_DIR / "视频-长视频替换-4x3加速版-ProPainter输入.json"
FIXED_REFERENCE = COMFY_INPUT / "25181125-唱歌优化-指定背景-1440x1080-v3.png"
# 动作迁移的默认人物参考图（未上传人物图时使用；迁移工作流内置参考图）
MIGRATE_REFERENCE = COMFY_INPUT / "singing_portrait_4x3_1440x1080.png"

# 批量预审的候选人物图：本地 ComfyUI Krea2「双图片编辑」（图像-1 场景造型 / 图像-2 身份）。
# 该节点常驻身份编辑 LoRA（Krea2-编辑identity_edit_v1_2），出图 ≈78s（RTX 3070 Ti 8GB + --lowvram）。
IMAGE_WORKFLOW_DIR = COMFY_ROOT / "user" / "default" / "workflows" / "image"
PORTRAIT_WORKFLOW = IMAGE_WORKFLOW_DIR / "图片-krea2-双图片编辑.json"
# ResolutionSelector 的画布档位：唱歌固定 4:3 横版、跳舞固定 9:16 竖版。
PORTRAIT_RATIO_PARAMS = {
    "4:3": {"aspect_ratio": "4:3 (Standard)", "megapixels": 1.0},
    "9:16": {"aspect_ratio": "9:16 (Portrait Widescreen)", "megapixels": 1.0},
}

# 构图规格：从用户的两张构图参考图实测得出，出图时注入提示词。
# 来源：`E:\AI_Exports\ComfyUI\Main\image\ca8aec93-…png`（4:3 唱歌参考，1448x1086）与
#      `f0a6e57c-…png`（9:16 跳舞参考，941x1672），2026-09-10 用视觉模型逐张测量。
# 注意：像素级的「主体上沿」自动测量在虚化背景上不可靠（把已知 5.5% 的身份图测成 18~24%），
# 所以这里用的是视觉模型的读数，而不是阈值法。
# ⚠️ 2026-09-20：小红书处罚事故后**试过把下边缘收到腰部/胸线以上，随后按用户口径改回**——
# 这两张图是喂 ComfyUI 的参考图（节点 `#30 LoadImage`），下装与手臂要靠它给，收太紧会让
# 迁移脑补下装；而"衣服不暴露"由 `batch_ai.PLATFORM_SAFE_BLOCK` 与 `judge_compliance`
# 在服装层解决，不需要靠裁掉身体。**数值保持原标定，别再动。**
PORTRAIT_COMPOSITION = {
    "4:3": (
        "画面比例 4:3 横版；头发最高点距上边缘 0%~1%（几乎贴边，允许轻微出框）；"
        "发际线距上边缘约 7%；脸部高度（发际线到下巴）约占画面高度 50%~55%；"
        "人物占画面高度接近 100%；下边缘切在胸部上方至上胸位置"
    ),
    "9:16": (
        "画面比例 9:16 竖版；头发最高点距上边缘约 1%；发际线距上边缘约 12%；"
        "脸部高度约占画面高度 34%；人物占画面高度约 99%；下边缘切至大腿上部"
    ),
}

# 高清档超分模型：
# - 9:16 迁移链路：512×896 → 1080×1920 仅需 ~2.1×，曾考虑用 x2plus（耗时约 1/4）
# - 4:3 迁移链路（512×384 → 1440×1080 需 ~2.8×）与唱歌链路（640×480 → 1440×1080）保持 x4plus
# - 独立「二采放大」路由固定 4×：只使用 x4plus，X2 常量保留备查（不再被路由使用）
UPSCALE_MODEL_X4 = "RealESRGAN_x4plus.pth"
UPSCALE_MODEL_X2 = "RealESRGAN_x2plus.pth"

# 二采放大 8GB 安全分批：与工作流 ① VHS_BatchManager「每批 8 帧」一致。
# 提交时按 ceil(源帧数 / 每批帧数) 预估总段数，运行时每个 VHS meta-batch
# 窗口（一次新 prompt 执行 = 处理一批帧）推进「放大 X/N」徽章。
UPSCALE_BATCH_FRAMES = 8

# 出图后的本地放大：中转站按 1k 档请求（`batch_image.image_sizes()`），出图后用 VOSR2 2×
# 放回 2k 档。人物图与两张封面共用同一条出图函数，所以这一处生效三处。
# 关掉本地放大（`H3_IMAGE_UPSCALE=off`）时，1k 的图会以原尺寸交付（封面仍会被缩放到
# TARGETS 尺寸，等于放大，会软）—— 所以正常情况下不要关。
IMAGE_UPSCALE_SCALE = 2
IMAGE_UPSCALE_SETTINGS = {
    "quality_profile": "speed", "tile_strategy": "auto", "tile_size": 512, "tile_overlap": 32,
    "vae_tile_size": 1024, "vae_tile_overlap": 32, "image_batch": 1, "frame_batch": 2,
    "dino_batch": 2, "temporal_cache": False, "cache_threshold": 0.003, "cache_refresh": 4,
    "memory_policy": "auto", "color_alignment": "wavelet",
}

# 动作迁移模型组合（博主 wan21_scail-2_loop 配置复刻）：
# - int8_convrot 主模型（RTX30 走 INT8 张量核，比 fp8 更省/更快）文件存在则自动启用
# - lightx2v 蒸馏 LoRA 用 rank64（博主两个版本都用 rank64，本机已存在）
SCAIL_UNET_INT8 = "wan2.1_14B_SCAIL_2_int8_convrot.safetensors"
LIGHTX2V_LORA_RANK64 = r"Wan2.1\lightx2v_I2V_14B_480p_cfg_step_distill_rank64_bf16.safetensors"
DIFFUSION_MODELS_DIR = COMFY_ROOT / "models" / "diffusion_models"

RVC_ROOT = COMFY_HOME / "RVC"
RVC_PYTHON = RVC_ROOT / ".venv" / "Scripts" / "python.exe"
RVC_SCRIPT = RVC_ROOT / "convert_video_to_my_voice.py"
# H3 唱歌链路默认音色：mogoo_v1（2026-09-11 用户要求从 wanwansu_v1 切换；用抖音博主
# 「Mogoo蘑菇」主页全部 198 个作品训练，193 条干声 / 74.9 分钟 / 1248 片段，
# 素材与再训流程归档在 E:\AI_Assets\RVC\Projects\mogoo_v1\）。
# 切音色前务必先跑该项目的 scripts\verify_speakers_channel.py（CAM++ 声纹校验）。
RVC_MODEL = RVC_ROOT / "assets" / "weights" / "mogoo_v1.pth"
# mogoo_v1 专属检索索引；文件缺失时转换脚本会退化为无索引模式。
RVC_INDEX = RVC_ROOT / "assets" / "indices" / "mogoo_v1.index"

# 剪映手书（内部名 JYgangbi）：剪映缓存字体；直出版默认 白字细描边·字号≈剪映字号10。
JY_SHOU_SHU_FONT = Path(
    os.getenv(
        "H3_JY_FONT",
        r"C:\Users\admin\AppData\Local\JianyingPro\User Data\Cache\effect\110450154\2e85dc679048a1da139661e050438ec2\JYgangbi.add_full_width_chars.ttf",
    )
)

# 去字幕字幕条定位：提交去字幕工作流前，用 backend/subtitle_detect.py（ComfyUI 自带
# python 运行，需 numpy/Pillow）对视频 30%~97% 时段抽帧、逐帧检测+跨帧投票，自动
# 定位持续显示的字幕条（跳过片头标题），再按 VHS 拉伸映射换算成画布遮罩坐标并
# 替换固定底部遮罩；检测失败或无结果时回退 CANVAS_PARAMS 的固定底部参数。
# 环境变量 H3_SUBTITLE_DETECT=0 可关闭自动检测（回到固定底部）。
SUBTITLE_DETECT = os.getenv("H3_SUBTITLE_DETECT", "1") != "0"
SUBTITLE_DETECT_SCRIPT = PROJECT_ROOT / "backend" / "subtitle_detect.py"

DB_PATH = DATA_DIR / "motionstudio.db"
COMFY_LOG = DATA_DIR / "comfyui.log"
MAX_DURATION_SECONDS = 40.0

# 歌曲生成 H3 分段（与工作流时长规划节点公式一致）：每段基础窗口 362 帧
# （@24fps ≈15.08 秒），段间以 22 帧相位对齐续接，实际每新增一段只多
# (362-22)=340 帧 ≈14.17 秒。分段预估与段位显示都基于这几组数字。
H3_FPS = 24
H3_CLIP_FRAMES = 362
H3_CONTEXT_FRAMES = 22

# 动作迁移路由：画布比例参数组。两份工作流 JSON 固定为 4:3 布局，9:16 时
# 程序把"读取/修复画布、底部字幕遮罩、SCAIL 生成宽高、1080P 输出尺寸"
# 替换为竖版数值 —— 与 SCAIL2-Easy 官方 512p 规则一致（短边对齐后长边对齐 32，
# 9:16 → 512×896；高清按抖音竖屏标准 1080×1920）。遮罩按作者 4:3 版贴底比例
# (中心 y=0.896H、高=0.151H、宽=0.84W) 等比换算，实测后如需微调改这里即可。
#
# clean_batch_frames / clean_batch_overlap：去字幕 ProPainter 的 8GB 显存分批参数。
# 单次执行常驻显存 ≈ 帧数 × 画布字节（视频 fp32 + 两套逐帧遮罩，512×896 约 9.2MB/帧），
# 叠加 ProPainter 内部 RAFT 每 12 帧的全分辨率相关体积尖峰（512×896 约 2.3GB）。
# 源帧数超过单批上限时，pipeline 按"每批 ≤ 上限帧 + 段间 overlap 帧重叠"拆段执行，
# 逐段裁掉重叠后拼接并回灌原音频；段间重叠保证边界处的时序上下文连续（拼缝不可见）。
CANVAS_PARAMS: dict[str, dict] = {
    "4:3": {
        "clean_width": 512,
        "clean_height": 384,
        "mask_center_x": 256,
        "mask_center_y": 344,
        "mask_width": 430,
        "mask_height": 58,
        "migrate_width": 512,
        "migrate_height": 384,
        "hd_width": 1440,
        "hd_height": 1080,
        "clean_batch_frames": 900,
        "clean_batch_overlap": 24,
    },
    "9:16": {
        "clean_width": 512,
        "clean_height": 896,
        "mask_center_x": 256,
        "mask_center_y": 803,
        "mask_width": 430,
        "mask_height": 135,
        "migrate_width": 512,
        "migrate_height": 896,
        "hd_width": 1080,
        "hd_height": 1920,
        "clean_batch_frames": 300,
        "clean_batch_overlap": 24,
    },
}
DEFAULT_CANVAS = "9:16"


def canvas_params(ratio: str) -> dict:
    params = CANVAS_PARAMS.get(ratio or "")
    if not params:
        raise ValueError(f"不支持的画布比例：{ratio!r}（可选 4:3 / 9:16）")
    return params


# 歌曲生成路由：画布比例参数组。唱歌工作流 JSON 固定为 4:3（640×480）布局，
# 运行时按比例替换五个 H3 分段节点（15/29/400/420/440）的生成宽高与
# ImageScaleToTotalPixels(269) 的参考图缩放档。9:16 采用 480×864（≈0.41MP，
# 短边 480 与横版同规格、宽高均为 32 倍数），比横版像素多约 35%；之后要高清
# 可直接用独立「二采放大」路由收 1080×1920。调数值只改这里。
SINGING_CANVAS_PARAMS: dict[str, dict] = {
    "4:3": {
        "sing_width": 640,
        "sing_height": 480,
        "megapixels": 0.31,  # 640×480 = 0.307MP
    },
    "9:16": {
        "sing_width": 480,
        "sing_height": 864,
        "megapixels": 0.41,  # 480×864 = 0.415MP
    },
}
DEFAULT_SINGING_CANVAS = "4:3"


def singing_canvas_params(ratio: str) -> dict:
    params = SINGING_CANVAS_PARAMS.get(ratio or "")
    if not params:
        raise ValueError(f"不支持的画布比例：{ratio!r}（可选 4:3 / 9:16）")
    return params


# 批量制作：画布比例是**每个条目单独选的**，不再按类型写死。
# 用户 2026-09-10 要求「每个视频需要让我选择比例；歌曲默认 4:3、跳舞默认 9:16，我可以改」，
# 所以这里只是新建批次时的默认值；用户在页面里逐条改，运行时以条目自己的 ratio 为准。
BATCH_RATIO_CHOICES = ("4:3", "9:16")
BATCH_DEFAULT_RATIOS = {"singing": "4:3", "dance": "9:16"}


def batch_default_ratio(kind: str) -> str:
    return BATCH_DEFAULT_RATIOS.get(kind, DEFAULT_SINGING_CANVAS)


def normalize_batch_ratio(ratio: str | None, kind: str = "singing") -> str:
    """校验并归一化批量条目的画布比例；空值时回落到该类型的默认比例。"""
    value = str(ratio or "").strip()
    if not value:
        return batch_default_ratio(kind)
    if value not in BATCH_RATIO_CHOICES:
        raise ValueError(f"不支持的画布比例：{value!r}（可选 4:3 / 9:16）")
    return value


def upscale_engine() -> str:
    """二采引擎：`esrgan`（默认，RealESRGAN 4×，行为与过去完全一致）或 `vosr2`。"""
    return (env_value("H3_UPSCALE_ENGINE") or "esrgan").strip().lower()


def upscale_engine_name() -> str:
    """二采引擎给用户看的短名：里程碑与日志统一用它，别写死 RealESRGAN。"""
    return "VOSR2" if upscale_engine() == "vosr2" else "RealESRGAN 4×"


def image_upscale_engine() -> str:
    """出图后是否本地放大：`vosr2`（默认，1k 档出图 → VOSR2 2× 回 2k 档）或 `off`。"""
    return (env_value("H3_IMAGE_UPSCALE") or "vosr2").strip().lower()


def image_identity_crop() -> bool:
    """身份图是否先裁成「头+肩」再送中转站（默认开）。

    身份原图是有事业线的深 V 照片，中转站按图片审核时，人物图这条（抽帧 + 身份图）比
    只发抽帧的封面更容易被 400 拦掉；提示词里图二只负责脸型五官，裁掉胸口不影响身份。
    `H3_IMAGE_IDENTITY_CROP=off` 可关掉（对照排查用）。
    """
    return (env_value("H3_IMAGE_IDENTITY_CROP") or "on").strip().lower() != "off"


def required_paths() -> dict[str, Path]:
    paths = {
        "ComfyUI Python": COMFY_PYTHON,
        "ComfyUI main.py": COMFY_MAIN,
        "唱歌工作流": SINGING_WORKFLOW,
        "高清工作流": UPSCALE_WORKFLOW,
        "去字幕工作流": CLEAN_WORKFLOW,
        "动作迁移工作流": MIGRATE_WORKFLOW,
        "固定人物图片": FIXED_REFERENCE,
        "RVC Python": RVC_PYTHON,
        "RVC 转换脚本": RVC_SCRIPT,
        "音色模型": RVC_MODEL,
    }
    if RVC_INDEX.is_file():
        paths["音色索引"] = RVC_INDEX
    # 切到 VOSR2 二采时，把那份工作流也纳入环境检查（缺了会在启动时就报出来，而不是等出片）
    if upscale_engine() == "vosr2":
        paths["VOSR2 二采工作流"] = UPSCALE_WORKFLOW_VOSR2
    # 动作迁移的默认人物图只在存在时校验：缺失时用户上传人物图即可
    if MIGRATE_REFERENCE.is_file():
        paths["动作迁移人物图"] = MIGRATE_REFERENCE
    return paths
