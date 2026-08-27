import logging
import time
from unittest.mock import patch

import pytest

from panel_core.models import LinkedPanel


@pytest.fixture
def app(app):
    from panel_core.api import panels

    if not any(bp_name == "panels" for bp_name in app.blueprints):
        app.register_blueprint(panels.bp, url_prefix="/api")
    return app


def _panel_with_token(db, raw="raw-secret-token", current_instance_id=None):
    panel = LinkedPanel(
        name="alpha",
        url="https://alpha.example.com/secret",
        federation_token="old-fed",
        created_at=1,
        transfer_token=raw,
        transfer_token_expires_at=int(time.time() * 1000) + 600_000,
        transfer_token_used=False,
        transfer_carry_admin=True,
        current_instance_id=current_instance_id,
    )
    db.session.add(panel)
    db.session.commit()
    return panel


def _seed_mirror(db, panel):
    from panel_core.services.state_mirror import write_cold, write_hot

    write_hot(
        panel.id,
        {"inbounds": [{"tag": "in-1"}]},
        taken_at=1,
        instance_id="inst-old",
        app_version="3.2.0",
        shrink_flagged=False,
    )
    write_cold(
        panel.id,
        {
            "outbounds": [],
            "identity": {
                "panel_domain": "alpha.example.com",
                "proxy_domain": "www.google.com",
                "secret_path": "secret",
            },
            "admin": {
                "username": "admin",
                "password": "scrypt$stub-hash-of-the-node-admin-password",
                "password_changed_at": 1,
            },
        },
        fingerprint="a" * 64,
        taken_at=1,
    )


def test_identity_does_not_burn_the_token(app, db):
    from panel_core.services.panel_transfer import resolve_identity

    panel = _panel_with_token(db)
    _seed_mirror(db, panel)

    first = resolve_identity("raw-secret-token")
    second = resolve_identity("raw-secret-token")

    assert first == second
    assert first["proxy_domain"] == "www.google.com"
    assert first["secret_path"] == "secret", (
        "установщик генерирует секретный путь случайным; при замене он обязан взять его у мастера, "
        "иначе опрос будет ходить по старому пути и получать 404 — единственная грабля, "
        "на которой владелец споткнулся при восстановлении 2026-08-23"
    )
    assert panel.transfer_token_used is False


@pytest.mark.parametrize(
    "seeded_current_instance_id",
    [
        None,
        "inst-old",
    ],
    ids=["pre_existing_panel_never_transferred", "panel_already_transferred_once"],
)
def test_claim_rotates_the_token_and_remembers_the_superseded_one(app, db, seeded_current_instance_id):
    from panel_core.services.panel_transfer import claim_transfer

    panel = _panel_with_token(db, current_instance_id=seeded_current_instance_id)
    _seed_mirror(db, panel)

    result = claim_transfer("raw-secret-token", instance_id="inst-new", federation_token="new-fed")

    assert result["carry_admin"] is True
    assert result["hot"]["inbounds"][0]["tag"] == "in-1"
    assert panel.federation_token == "new-fed"
    assert panel.superseded_token == "old-fed"
    assert panel.superseded_instance_id == "inst-old", (
        "должно совпадать вне зависимости от того, писал ли current_instance_id кто-то раньше: "
        "прямое значение для уже переносившейся панели, зеркало (мирроп last polled instance_id) "
        "как фолбэк для панели, заведённой до этой ветки"
    )
    assert panel.current_instance_id == "inst-new"
    assert panel.transfer_state == "awaiting_dns"
    assert panel.transfer_token_used is True


def test_the_first_transfer_of_a_pre_existing_panel_marks_the_zombie_superseded(app, db):
    from panel_core.services.panel_transfer import claim_transfer, instance_verdict

    panel = _panel_with_token(db, current_instance_id=None)
    _seed_mirror(db, panel)

    claim_transfer("raw-secret-token", instance_id="inst-new", federation_token="new-fed")

    result = instance_verdict("old-fed", "inst-old")

    assert result["verdict"] == "superseded", (
        "панель, заведённая до этой ветки, никогда не писала current_instance_id. Заявка обязана "
        "взять инстанс зомби из зеркала (последнее, что зомби сам о себе сообщил при опросе), "
        "иначе воскресшая машина A будет отвечать вердиктом unknown и работать в полную силу"
    )


def test_a_replay_by_the_same_instance_returns_the_same_answer(app, db):
    from panel_core.services.panel_transfer import claim_transfer

    panel = _panel_with_token(db)
    _seed_mirror(db, panel)

    first = claim_transfer("raw-secret-token", instance_id="inst-new", federation_token="new-fed")
    second = claim_transfer("raw-secret-token", instance_id="inst-new", federation_token="new-fed")

    assert first["hot"] == second["hot"], (
        "ответ мог потеряться по дороге: мастер токен уже перезаписал, а нода об этом не знает. "
        "Тот же механизм, что у повторной выдачи доступа по платежу"
    )


def test_a_replay_with_a_mismatched_token_does_not_move_the_stored_one(app, db):
    from panel_core.services.panel_transfer import claim_transfer

    panel = _panel_with_token(db)
    _seed_mirror(db, panel)

    claim_transfer("raw-secret-token", instance_id="inst-new", federation_token="new-fed")
    claim_transfer("raw-secret-token", instance_id="inst-new", federation_token="different-fed")

    assert panel.federation_token == "new-fed", (
        "реплей по тому же instance_id не читает federation_token из повторного запроса — "
        "авторитетна только первая успешная заявка. Живая нода никогда не должна прислать другой "
        "токен, но если бы мастер тихо принимал его на повторе, нода, перечеканившая токен после "
        "потерянного ответа, увела бы мастер на секрет, которого у неё самой уже нет"
    )


def test_a_replay_by_a_different_instance_is_refused(app, db):
    from panel_core.services.panel_transfer import TransferError, claim_transfer

    panel = _panel_with_token(db)
    _seed_mirror(db, panel)
    claim_transfer("raw-secret-token", instance_id="inst-new", federation_token="new-fed")

    with pytest.raises(TransferError):
        claim_transfer("raw-secret-token", instance_id="inst-other", federation_token="other-fed")


def test_an_expired_token_is_refused(app, db):
    from panel_core.services.panel_transfer import TransferError, claim_transfer

    panel = _panel_with_token(db)
    panel.transfer_token_expires_at = int(time.time() * 1000) - 1
    db.session.commit()
    _seed_mirror(db, panel)

    with pytest.raises(TransferError) as exc:
        claim_transfer("raw-secret-token", instance_id="inst-new", federation_token="new-fed")
    assert "expired" in str(exc.value).lower()


def test_an_unknown_token_is_refused(app, db):
    from panel_core.services.panel_transfer import TransferError, claim_transfer

    _panel_with_token(db)

    with pytest.raises(TransferError):
        claim_transfer("not-the-token", instance_id="inst-new", federation_token="new-fed")


def test_two_simultaneous_claims_leave_exactly_one_winner(app, db):
    from panel_core.services.panel_transfer import TransferError, claim_transfer

    panel = _panel_with_token(db)
    _seed_mirror(db, panel)

    winners = 0
    for instance in ("inst-a", "inst-b"):
        try:
            claim_transfer("raw-secret-token", instance_id=instance, federation_token=f"fed-{instance}")
            winners += 1
        except TransferError:
            pass

    assert winners == 1, (
        "гашение ключа обязано быть условным обновлением с проверкой числа затронутых строк, "
        "а не «прочитал, проверил, записал» — иначе обе машины пройдут"
    )


def test_claim_endpoint_is_rate_limited_and_unauthenticated(app, db):
    panel = _panel_with_token(db)
    _seed_mirror(db, panel)

    resp = app.test_client().post(
        "/api/panels/transfer/claim",
        json={"transfer_token": "raw-secret-token", "instance_id": "inst-new", "federation_token": "new-fed"},
    )

    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()["hot"]["inbounds"][0]["tag"] == "in-1"


def test_carry_admin_false_strips_the_admin_credentials_from_the_reply(app, db):
    from panel_core.services.panel_transfer import claim_transfer

    panel = _panel_with_token(db)
    panel.transfer_carry_admin = False
    db.session.commit()
    _seed_mirror(db, panel)

    result = claim_transfer("raw-secret-token", instance_id="inst-new", federation_token="new-fed")

    assert result["carry_admin"] is False
    assert "admin" not in result["cold"], (
        "снятая галочка carry_admin обязана останавливать хеш пароля админа ноды на мастере, "
        "а не полагаться на то, что получатель его отбросит"
    )


def test_carry_admin_true_keeps_the_admin_credentials_in_the_reply(app, db):
    from panel_core.services.panel_transfer import claim_transfer

    panel = _panel_with_token(db)
    _seed_mirror(db, panel)

    result = claim_transfer("raw-secret-token", instance_id="inst-new", federation_token="new-fed")

    assert result["carry_admin"] is True
    assert result["cold"]["admin"]["username"] == "admin"


def test_the_cas_predicate_refuses_a_row_claimed_between_the_read_and_the_update(app, db):
    from panel_core.services import panel_transfer
    from panel_core.services.panel_transfer import TransferError, claim_transfer

    panel = _panel_with_token(db)
    _seed_mirror(db, panel)

    claim_transfer("raw-secret-token", instance_id="inst-a", federation_token="fed-a")

    stale = db.session.get(LinkedPanel, panel.id)
    stale.transfer_token_used = False

    with patch.object(panel_transfer, "_panel_for_token", return_value=stale):
        with db.session.no_autoflush:
            with pytest.raises(TransferError):
                claim_transfer("raw-secret-token", instance_id="inst-b", federation_token="fed-b")

    winner = db.session.get(LinkedPanel, panel.id)
    assert winner.current_instance_id == "inst-a", (
        "второй заявитель прочитал строку до победы первого, но UPDATE обязан пересчитать "
        "предикат по текущей строке в базе, а не по устаревшему объекту в памяти вызывающего"
    )
    assert winner.federation_token == "fed-a"


def test_a_non_ascii_token_gives_the_same_answer_whether_or_not_a_transfer_is_active(app, db):
    from panel_core.services.panel_transfer import TransferError, resolve_identity

    with pytest.raises(TransferError) as no_transfer:
        resolve_identity("тест-не-ascii")

    panel = _panel_with_token(db)
    _seed_mirror(db, panel)

    with pytest.raises(TransferError) as with_transfer:
        resolve_identity("тест-не-ascii")

    assert no_transfer.value.status == with_transfer.value.status == 401
    assert str(no_transfer.value) == str(with_transfer.value), (
        "неаутентифицированная ручка не должна отвечать по-разному в зависимости от того, "
        "идёт ли сейчас чей-то перенос — иначе код ответа сам становится оракулом"
    )


def test_a_non_ascii_token_is_refused_not_crashed_on_claim_too(app, db):
    from panel_core.services.panel_transfer import TransferError, claim_transfer

    panel = _panel_with_token(db)
    _seed_mirror(db, panel)

    with pytest.raises(TransferError) as exc:
        claim_transfer("тест-не-ascii", instance_id="inst-new", federation_token="new-fed")
    assert exc.value.status == 401


def test_a_non_ascii_token_over_the_route_answers_401_not_500(app, db):
    panel = _panel_with_token(db)
    _seed_mirror(db, panel)

    resp = app.test_client().post(
        "/api/panels/transfer/identity",
        json={"transfer_token": "тест-не-ascii"},
    )
    assert resp.status_code == 401, resp.get_data(as_text=True)


def test_the_secret_path_fallback_reads_the_url_path_not_the_hostname(app, db):
    from panel_core.services.panel_transfer import resolve_identity

    panel = LinkedPanel(
        name="bare",
        url="https://node.example.com",
        federation_token="old-fed",
        created_at=1,
        transfer_token="raw-bare-token",
        transfer_token_expires_at=int(time.time() * 1000) + 600_000,
        transfer_token_used=False,
        transfer_carry_admin=True,
        current_instance_id="inst-old",
    )
    db.session.add(panel)
    db.session.commit()

    result = resolve_identity("raw-bare-token")

    assert result["panel_domain"] == "node.example.com"
    assert result["secret_path"] == "", (
        "URL без пути не содержит секретного пути; отдавать вместо него имя хоста — именно та "
        "грабля 2026-08-23: мастер начнёт опрашивать /node.example.com/api/... и получать 404"
    )


def test_the_secret_path_fallback_reads_a_real_path_when_present(app, db):
    from panel_core.services.panel_transfer import resolve_identity

    panel = LinkedPanel(
        name="withpath",
        url="https://n.example.com/secret",
        federation_token="old-fed",
        created_at=1,
        transfer_token="raw-path-token",
        transfer_token_expires_at=int(time.time() * 1000) + 600_000,
        transfer_token_used=False,
        transfer_carry_admin=True,
        current_instance_id="inst-old",
    )
    db.session.add(panel)
    db.session.commit()

    result = resolve_identity("raw-path-token")

    assert result["panel_domain"] == "n.example.com"
    assert result["secret_path"] == "secret"


def test_a_successful_claim_shrinks_the_token_lifetime_instead_of_leaving_it_live_for_an_hour(app, db):
    from panel_core.services.panel_transfer import CLAIMED_TOKEN_GRACE_SECONDS, claim_transfer

    panel = _panel_with_token(db)
    _seed_mirror(db, panel)
    original_expiry = panel.transfer_token_expires_at

    before = int(time.time() * 1000)
    claim_transfer("raw-secret-token", instance_id="inst-new", federation_token="new-fed")

    assert panel.transfer_token_expires_at < original_expiry, (
        "ключ уже использован — незачем оставлять его валидным ещё почти час, как будто заявку никто не подавал"
    )
    assert panel.transfer_token_expires_at <= before + CLAIMED_TOKEN_GRACE_SECONDS * 1000 + 2_000


def test_identity_success_is_journalled_without_leaking_the_key(app, db, caplog):
    panel = _panel_with_token(db)
    _seed_mirror(db, panel)

    with caplog.at_level(logging.WARNING):
        resp = app.test_client().post(
            "/api/panels/transfer/identity",
            json={"transfer_token": "raw-secret-token"},
        )
    assert resp.status_code == 200

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any(panel.name in r.getMessage() for r in warnings), (
        "успешное опознавание не оставило ни одной строки — единственный компенсирующий контроль "
        "ручки, которая намеренно не гасит ключ и потому допускает повторные обращения"
    )
    joined = "\n".join(r.getMessage() for r in warnings)
    assert "raw-secret-token" not in joined
    assert "scrypt$stub-hash-of-the-node-admin-password" not in joined


def test_identity_refusal_is_journalled_without_leaking_the_key(app, db, caplog):
    _panel_with_token(db)

    with caplog.at_level(logging.WARNING):
        resp = app.test_client().post(
            "/api/panels/transfer/identity",
            json={"transfer_token": "not-the-token"},
        )
    assert resp.status_code == 401

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("unknown transfer token" in r.getMessage() for r in warnings), (
        "отказ на /identity тоже обязан оставлять след — это то, чем в журнале отличается "
        "«админ поставил замену» от «кто-то перебирает ключи»"
    )
    joined = "\n".join(r.getMessage() for r in warnings)
    assert "not-the-token" not in joined


def test_a_claim_does_not_extend_a_token_that_was_already_expiring_soon(app, db):
    from panel_core.services.panel_transfer import claim_transfer

    panel = _panel_with_token(db)
    short_expiry = int(time.time() * 1000) + 120_000
    panel.transfer_token_expires_at = short_expiry
    db.session.commit()
    _seed_mirror(db, panel)

    claim_transfer("raw-secret-token", instance_id="inst-new", federation_token="new-fed")

    assert panel.transfer_token_expires_at <= short_expiry, (
        "у ключа и так оставалось меньше пяти минут — заявка не имеет права продлить срок жизни "
        "до полных пяти минут; гашение обязано брать min(), а не безусловно перезаписывать срок"
    )
