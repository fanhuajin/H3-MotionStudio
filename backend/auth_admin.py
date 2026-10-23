"""Local interactive password reset: python -m backend.auth_admin."""
import getpass
import secrets

from .auth import store


def main():
    if not store.configured():
        raise SystemExit("尚未创建管理员，请先在本机页面完成首次设置。")
    password = getpass.getpass("新密码（至少 12 位）：")
    if not 12 <= len(password) <= 256:
        raise SystemExit("密码长度必须为 12 至 256 位。")
    if password != getpass.getpass("再次输入新密码："):
        raise SystemExit("两次密码不一致，未做修改。")
    salt = secrets.token_hex(16)
    digest = store.digest(password, salt)
    from contextlib import closing
    with closing(store.connect()) as connection, connection:
        connection.execute("UPDATE owner SET salt=?, digest=? WHERE id=1", (salt, digest))
        connection.execute("DELETE FROM sessions")
    print("密码已重置，全部登录会话已撤销。")


if __name__ == "__main__":
    main()
