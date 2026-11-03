"""管理员运维 CLI：账号自举与救援通道。

为什么注册接口不提供"管理员"选项：
- 允许自助注册管理员是最典型的越权漏洞，所以注册只能选运营/审核员/观察者；
- 管理员只能由**现有管理员**在「成员管理」里分配。

但这会带来一个死锁：首个管理员密码丢失、或者部署后库里没有任何 admin 时，
人就彻底进不去了。这个 CLI 就是那把"物理钥匙"——只能在服务器本机执行，
不暴露任何 HTTP 接口，也不参与线上请求链路。

用法（在 backend 目录下执行）：
    python -m app.admin_cli list
    python -m app.admin_cli promote <username>
    python -m app.admin_cli demote <username>
    python -m app.admin_cli reset-password <username> [--password 新密码]
    python -m app.admin_cli create-admin <username> [--display 名称] [--password 密码]
"""
import argparse
import secrets
import string
import sys

from . import config
from .auth import AuthStore, ROLE_LABELS


def _store() -> AuthStore:
    return AuthStore(config.WORKSPACE_DB)


def _find(store: AuthStore, username: str) -> dict:
    target = username.strip().lower()
    for user in store.list_users():
        if user["username"] == target:
            return user
    raise SystemExit(f"找不到用户 {username!r}，先执行 `python -m app.admin_cli list` 看现有账号")


def _random_password(length: int = 16) -> str:
    """生成满足策略（含字母+数字）的随机密码。"""
    alphabet = string.ascii_letters + string.digits
    while True:
        value = "".join(secrets.choice(alphabet) for _ in range(length))
        if any(c.isalpha() for c in value) and any(c.isdigit() for c in value):
            return value


def _error_detail(exc: Exception) -> str:
    return getattr(exc, "detail", None) or str(exc)


def cmd_list(_args) -> None:
    users = _store().list_users()
    if not users:
        print("库里还没有任何账号。启动服务后第一个注册的账号会自动成为管理员。")
        return
    print(f"{'用户名':<20}{'角色':<10}{'状态':<6}显示名")
    print("-" * 60)
    for user in users:
        role = ROLE_LABELS.get(user["role"], user["role"])
        state = "启用" if user["active"] else "停用"
        print(f"{user['username']:<20}{role:<10}{state:<6}{user['display_name']}")
    active_admins = [u for u in users if u["role"] == "admin" and u["active"]]
    print("-" * 60)
    print(f"共 {len(users)} 个账号，可用管理员 {len(active_admins)} 个。")
    if not active_admins:
        print("⚠ 当前没有可用管理员：用 promote 提升一个账号，否则没人能进「成员管理」。")


def cmd_promote(args) -> None:
    store = _store()
    user = _find(store, args.username)
    if user["role"] == "admin":
        print(f"{user['username']} 已经是管理员，无需操作。")
        return
    store.update_user(user["id"], role="admin")
    print(f"已把 {user['username']}（{user['display_name']}）提升为管理员。")
    print("角色是每次请求实时读库的，刷新页面即可看到「成员管理」入口。")


def cmd_demote(args) -> None:
    store = _store()
    user = _find(store, args.username)
    if user["role"] != "admin":
        print(f"{user['username']} 当前不是管理员（{ROLE_LABELS.get(user['role'], user['role'])}）。")
        return
    try:
        # update_user 内置了"不能降级最后一个管理员"的保护，这里直接依赖它
        store.update_user(user["id"], role=args.role)
    except Exception as exc:  # HTTPException(409/422) 等
        raise SystemExit(f"降级失败：{_error_detail(exc)}")
    print(f"已把 {user['username']} 降级为{ROLE_LABELS.get(args.role, args.role)}。")


def cmd_reset_password(args) -> None:
    store = _store()
    user = _find(store, args.username)
    password = args.password or _random_password()
    store.reset_password(user["id"], password)
    print(f"已重置 {user['username']} 的密码：{password}")
    print("该账号的所有登录会话已失效，请用新密码重新登录。")


def cmd_create_admin(args) -> None:
    store = _store()
    password = args.password or _random_password()
    try:
        user = store.create_user(
            username=args.username,
            display_name=args.display or args.username,
            password=password,
            role="admin",
            created_by="admin-cli",
        )
    except Exception as exc:
        raise SystemExit(f"创建失败：{_error_detail(exc)}")
    print(f"已创建管理员 {user['username']}（{user['display_name']}），初始密码：{password}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.admin_cli",
        description="ContentOps 账号运维：查看、提升管理员、重置密码",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="列出所有账号与角色").set_defaults(func=cmd_list)

    p_promote = sub.add_parser("promote", help="把现有账号提升为管理员")
    p_promote.add_argument("username")
    p_promote.set_defaults(func=cmd_promote)

    p_demote = sub.add_parser("demote", help="把管理员降级为其他角色")
    p_demote.add_argument("username")
    p_demote.add_argument(
        "--role", default="operator", choices=["operator", "reviewer", "viewer"],
        help="降级后的角色，默认 operator",
    )
    p_demote.set_defaults(func=cmd_demote)

    p_reset = sub.add_parser("reset-password", help="重置账号密码并踢掉旧会话")
    p_reset.add_argument("username")
    p_reset.add_argument("--password", help="不传则随机生成 16 位密码")
    p_reset.set_defaults(func=cmd_reset_password)

    p_create = sub.add_parser("create-admin", help="直接创建一个管理员账号")
    p_create.add_argument("username")
    p_create.add_argument("--display", help="显示名称，默认与用户名相同")
    p_create.add_argument("--password", help="不传则随机生成 16 位密码")
    p_create.set_defaults(func=cmd_create_admin)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
