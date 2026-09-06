from __future__ import annotations

import os
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

COMFY_HOME = Path(os.getenv("H3_COMFY_HOME", r"D:\Comfyui")).resolve()
COMFY_ROOT = COMFY_HOME / "ComfyUI"
COMFY_INPUT = COMFY_ROOT / "input"
COMFY_OUTPUT = COMFY_ROOT / "output"
COMFY_PYTHON = COMFY_HOME / "python_embeded" / "python.exe"
COMFY_MAIN = COMFY_ROOT / "main.py"
COMFY_URL = os.getenv("H3_COMFY_URL", "http://127.0.0.1:8188").rstrip("/")
COMFY_WS = COMFY_URL.replace("http://", "ws://").replace("https://", "wss://")

WORKFLOW_DIR = COMFY_ROOT / "user" / "default" / "workflows" / "video"
SINGING_WORKFLOW = WORKFLOW_DIR / "视频-单图唱歌-自动拼接40秒内-4x3-运镜版.json"
UPSCALE_WORKFLOW = WORKFLOW_DIR / "视频-成片输入-独立二采-RealESRGAN4x转1080P-8GB高清加强版.json"
# 动作迁移链路：先去字幕（ProPainter 固定底部），再长视频替换/动作迁移
CLEAN_WORKFLOW = WORKFLOW_DIR / "视频-去字幕-ProPainter-固定底部.json"
MIGRATE_WORKFLOW = WORKFLOW_DIR / "视频-长视频替换-4x3加速版-ProPainter输入.json"
FIXED_REFERENCE = COMFY_INPUT / "25181125-唱歌优化-指定背景-1440x1080-v3.png"
# 动作迁移的默认人物参考图（未上传人物图时使用；迁移工作流内置参考图）
MIGRATE_REFERENCE = COMFY_INPUT / "singing_portrait_4x3_1440x1080.png"

# 高清档超分模型：
# - 9:16 迁移链路：512×896 → 1080×1920 仅需 ~2.1×，用 x2plus（约 1/4 耗时，画质几乎无差）
# - 4:3 迁移链路（512×384 → 1440×1080 需 ~2.8×）与唱歌链路（640×480 → 1440×1080）保持 x4plus
UPSCALE_MODEL_X4 = "RealESRGAN_x4plus.pth"
UPSCALE_MODEL_X2 = "RealESRGAN_x2plus.pth"

# 二采放大 8GB 安全分批：与工作流 ① VHS_BatchManager「每批 8 帧」一致。
# 提交时按 ceil(源帧数 / 每批帧数) 预估总段数，运行时每个 VHS meta-batch
# 窗口（一次新 prompt 执行 = 处理一批帧）推进「放大 X/N」徽章。
UPSCALE_BATCH_FRAMES = 8

# 动作迁移模型组合（博主 wan21_scail-2_loop 配置复刻）：
# - int8_convrot 主模型（RTX30 走 INT8 张量核，比 fp8 更省/更快）文件存在则自动启用
# - lightx2v 蒸馏 LoRA 用 rank64（博主两个版本都用 rank64，本机已存在）
SCAIL_UNET_INT8 = "wan2.1_14B_SCAIL_2_int8_convrot.safetensors"
LIGHTX2V_LORA_RANK64 = r"Wan2.1\lightx2v_I2V_14B_480p_cfg_step_distill_rank64_bf16.safetensors"
DIFFUSION_MODELS_DIR = COMFY_ROOT / "models" / "diffusion_models"

RVC_ROOT = COMFY_HOME / "RVC"
RVC_PYTHON = RVC_ROOT / ".venv" / "Scripts" / "python.exe"
RVC_SCRIPT = RVC_ROOT / "convert_video_to_my_voice.py"
# H3 唱歌链路默认音色。2026-09-06 两轮 A/B（11 模型全量对比 + kikiV1/SANHEYIV1 正面 PK）
# 定稿：原 ranran（无 index、频谱 1k-3k 仅 ~10%，输出"闷/呜 u"）弃用，改 kikiV1——
# 40k 标注与推理一致、音高保真 100%/相关性 0.995、频谱明亮（1k-3k ~20%）、社区"常用"款，
# 索引 kikiV1.index 与模型同源（上游 挑选/!kikiV1 发布目录）。ranran 无专属索引，
# 已按音色库整理规则从 assets/weights 移除（保留该行历史说明，勿再放入无索引模型）。
RVC_MODEL = RVC_ROOT / "assets" / "weights" / "kikiV1.pth"
# kikiV1 官方索引（ASCII 文件名，faiss 可正常读取）；文件被删则自动以无索引模式运行。
RVC_INDEX = RVC_ROOT / "assets" / "indices" / "kikiV1.index"

# 歌词字幕路由（多语言）：用 RVC 环境的 torchaudio HDEMUCS 分离人声，再用
# faster-whisper（本地模型目录，缺省 D:\tmp\fw-turbo，可用 H3_WHISPER_MODEL 覆盖）
# 自动识别语种并实测每句演唱时间，官方歌词只取文本（韩/日/中/英均可）。
# 2026-09-06 升级史：base 对带伴奏演唱错字/幻觉整句 → small；随后识别时注入
# 官方歌词 initial_prompt（见 lyrics_stage.py --prompt-file）后 small 已基本可用，
# 又试 large-v3-turbo —— 但 turbo 对本机这类 AI 翻唱人声不稳定：CPU int8 长跑会
# 间歇性整段空转，GPU（cuBLAS/cuDNN 已装入 RVC venv）上则会出现复读死循环
# （实测「续续续…」×90）。fw-small 连跑多次文本逐字一致、词级时间正确。
# 因此默认 fw-small；GPU 可用时 lyrics_stage.py 自动走 CUDA（几秒完成），
# 无需换模型。显式设 H3_WHISPER_MODEL 可覆盖（turbo 需自行评估稳定性）。
LYRICS_ASR_PY = RVC_PYTHON
LYRICS_ASR_MODEL = Path(os.getenv("H3_WHISPER_MODEL", r"D:\tmp\fw-small"))
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
    # 动作迁移的默认人物图只在存在时校验：缺失时用户上传人物图即可
    if MIGRATE_REFERENCE.is_file():
        paths["动作迁移人物图"] = MIGRATE_REFERENCE
    return paths

