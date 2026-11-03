"""运维 CLI 的回归测试：账号自举、提升、降级保护与密码重置。

这些命令是"没有管理员时唯一的入口"，一旦改坏就把人锁在门外，
所以必须和 HTTP 接口一样有测试兜底。
"""
import pytest

from app import admin_cli, config
from app.auth import AuthStore


def _seed():
    """直接建库：root 是管理员，bob 是运营（跳过 HTTP 层，专注 CLI 行为）。"""
    store = AuthStore(config.WORKSPACE_DB)
    root = store.create_user(
        username="root", display_name="根管理员",
        password="RootPass123", role="admin")
    bob = store.create_user(
        username="bob", display_name="小鲍",
        password="BobPass123", role="operator")
    return store, root, bob


def _role_of(store, username):
    return next(u for u in store.list_users() if u["username"] == username)["role"]


def test_cli_list_shows_roles(capsys):
    store, _, _ = _seed()
    admin_cli.main(["list"])
    out = capsys.readouterr().out
    assert "root" in out and "管理员" in out
    assert "bob" in out and "内容运营" in out
    assert "可用管理员 1 个" in out


def test_cli_promote_then_role_is_admin(capsys):
    store, _, _ = _seed()
    assert _role_of(store, "bob") == "operator"

    admin_cli.main(["promote", "bob"])
    out = capsys.readouterr().out
    assert "提升为管理员" in out

    # 重新读库确认落盘，而不是只改了内存对象
    assert _role_of(AuthStore(config.WORKSPACE_DB), "bob") == "admin"


def test_cli_promote_is_idempotent(capsys):
    _seed()
    admin_cli.main(["promote", "root"])
    assert "已经是管理员" in capsys.readouterr().out


def test_cli_promote_unknown_user_exits():
    _seed()
    with pytest.raises(SystemExit):
        admin_cli.main(["promote", "nobody"])


def test_cli_refuses_to_demote_last_admin():
    _seed()
    with pytest.raises(SystemExit):
        admin_cli.main(["demote", "root"])
    # 保护生效后 root 必须还是管理员，不能把自己锁在门外
    assert _role_of(AuthStore(config.WORKSPACE_DB), "root") == "admin"


def test_cli_demote_allowed_when_another_admin_exists(capsys):
    _seed()
    admin_cli.main(["promote", "bob"])
    admin_cli.main(["demote", "root"])
    assert "降级" in capsys.readouterr().out
    assert _role_of(AuthStore(config.WORKSPACE_DB), "root") == "operator"


def test_cli_reset_password_replaces_old_credential(capsys):
    store, _, bob = _seed()
    created = store.create_user(
        username="carol", display_name="卡罗",
        password="CarolPass123", role="operator")
    assert created["username"] == "carol"

    admin_cli.main(["reset-password", "carol", "--password", "NewPass456"])
    out = capsys.readouterr().out
    assert "NewPass456" in out

    store = AuthStore(config.WORKSPACE_DB)
    assert store.authenticate("carol", "NewPass456")["username"] == "carol"
    with pytest.raises(Exception):
        store.authenticate("carol", "CarolPass123")


def test_cli_create_admin(capsys):
    _seed()
    admin_cli.main(["create-admin", "ops2", "--display", "运维二号", "--password", "Ops2Pass123"])
    assert "已创建管理员" in capsys.readouterr().out
    store = AuthStore(config.WORKSPACE_DB)
    assert _role_of(store, "ops2") == "admin"
    assert store.authenticate("ops2", "Ops2Pass123")["display_name"] == "运维二号"
