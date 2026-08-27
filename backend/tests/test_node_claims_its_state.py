import base64
from unittest.mock import patch

import pytest

from panel_core.models import Client, FederationConfig, Inbound, SystemSetting


def _token(url="https://master.example.com/ms", secret="raw-secret"):
    return base64.urlsafe_b64encode(f"{url}|{secret}".encode()).decode().rstrip("=")


_REPLY = {
    "hot": {
        "inbounds": [
            {
                "id": 1,
                "tag": "in-1",
                "port": 443,
                "protocol": "vless",
                "stream_settings": {},
                "clients": [{"id": "uuid-1", "email": "a@b", "inbound_tag": "in-1", "expiry_time": 0}],
            }
        ]
    },
    "cold": {
        "outbounds": [],
        "routing_profiles": [],
        "balancers": [],
        "settings": [],
        "receipts": [],
        "notification_logs": [],
        "admin": None,
        "identity": {"panel_domain": "alpha.example.com", "proxy_domain": "www.google.com", "secret_path": "s"},
    },
    "carry_admin": False,
    "taken_at": 1,
}


def test_the_token_carries_the_master_url(app, db):
    from panel_core.services.master_client import decode_transfer_token

    url, secret = decode_transfer_token(_token())
    assert url == "https://master.example.com/ms"
    assert secret == "raw-secret"


def test_claim_applies_the_state_and_marks_itself_done(app, db, monkeypatch):
    from panel_core.jobs import transfer as job
    from panel_core.services.master_client import CLAIMED_SETTING_KEY

    monkeypatch.setenv("NODE_TRANSFER_TOKEN", _token())

    with patch.object(job, "MasterClient") as client_cls:
        client_cls.return_value.claim.return_value = _REPLY
        job.claim_state_job()

    assert Inbound.query.count() == 1
    assert db.session.get(Client, "uuid-1") is not None
    assert db.session.get(SystemSetting, CLAIMED_SETTING_KEY).value == "1"


def test_a_second_run_does_nothing(app, db, monkeypatch):
    from panel_core.jobs import transfer as job

    monkeypatch.setenv("NODE_TRANSFER_TOKEN", _token())

    with patch.object(job, "MasterClient") as client_cls:
        client_cls.return_value.claim.return_value = _REPLY
        job.claim_state_job()
        job.claim_state_job()

        assert client_cls.return_value.claim.call_count == 1, (
            "заявка одноразовая: повторный запрос вернул бы отказ и засорял бы лог каждые 30 секунд"
        )


def test_no_token_means_no_claim(app, db, monkeypatch):
    from panel_core.jobs import transfer as job

    monkeypatch.delenv("NODE_TRANSFER_TOKEN", raising=False)

    with patch.object(job, "MasterClient") as client_cls:
        job.claim_state_job()

    assert client_cls.called is False
    assert Inbound.query.count() == 0


def test_an_unreachable_master_leaves_the_node_empty_and_retries(app, db, monkeypatch):
    from panel_core.jobs import transfer as job
    from panel_core.services.master_client import CLAIMED_SETTING_KEY

    monkeypatch.setenv("NODE_TRANSFER_TOKEN", _token())

    with patch.object(job, "MasterClient") as client_cls:
        client_cls.return_value.claim.side_effect = RuntimeError("unreachable")
        job.claim_state_job()

    assert db.session.get(SystemSetting, CLAIMED_SETTING_KEY) is None, (
        "недоступный мастер — не повод считать перенос состоявшимся; задача повторит через 30 секунд"
    )


def test_the_node_stores_its_new_federation_link(app, db, monkeypatch):
    from panel_core.jobs import transfer as job

    monkeypatch.setenv("NODE_TRANSFER_TOKEN", _token())

    with patch.object(job, "MasterClient") as client_cls:
        client_cls.return_value.claim.return_value = _REPLY
        job.claim_state_job()

    cfg = db.session.get(FederationConfig, 1)
    assert cfg is not None and cfg.federation_token, (
        "токен чеканит нода и отправляет мастеру в заявке; свою копию она обязана сохранить, "
        "иначе следующий же опрос от мастера получит 401"
    )
    assert cfg.master_url == "https://master.example.com/ms"


def test_a_lost_reply_reuses_the_same_federation_token_on_retry(app, db, monkeypatch):
    from panel_core.jobs import transfer as job

    monkeypatch.setenv("NODE_TRANSFER_TOKEN", _token())

    with patch.object(job, "MasterClient") as client_cls:
        client_cls.return_value.claim.side_effect = [RuntimeError("read timed out"), _REPLY]
        job.claim_state_job()
        job.claim_state_job()

    calls = client_cls.return_value.claim.call_args_list
    assert len(calls) == 2
    assert calls[0].args[2] == calls[1].args[2], (
        "мастер гасит ключ переноса и запоминает federation_token из первого успешного запроса; "
        "ветка реплея этот аргумент на повторе не читает. Если нода на втором тике чеканит новый "
        "токен вместо того чтобы переиспользовать сохранённый, у неё и у мастера остаются разные "
        "копии, и панель навсегда отвечает 401"
    )


def test_a_broken_config_generation_skips_the_restart_but_keeps_the_claim(app, db, monkeypatch, caplog):
    from panel_core.jobs import transfer as job
    from panel_core.services.master_client import CLAIMED_SETTING_KEY

    monkeypatch.setenv("NODE_TRANSFER_TOKEN", _token())

    with (
        patch.object(job, "MasterClient") as client_cls,
        patch("panel_core.xray.facade.generate_config_file", side_effect=RuntimeError("disk full")) as mock_gen,
        patch("panel_core.xray.facade.restart_xray_container") as mock_restart,
        caplog.at_level("WARNING", logger="panel_core.jobs.transfer"),
    ):
        client_cls.return_value.claim.return_value = _REPLY
        job.claim_state_job()

    mock_gen.assert_called_once()
    assert mock_restart.call_count == 0, (
        "перезапускать Xray со старым (не перезаписанным) конфигом бессмысленно — синк целиком "
        "должен повториться на следующем тике"
    )
    assert db.session.get(SystemSetting, CLAIMED_SETTING_KEY).value == "1", (
        "провал генерации конфига — это отдельная, локальная проблема; данные уже приехали и "
        "закоммичены, откатывать состоявшийся перенос из-за неё нельзя"
    )
    messages = [rec.message for rec in caplog.records]
    assert any("config could not be regenerated" in m for m in messages), (
        "провал generate_config_file() обязан попасть в лог отдельной строкой, а не молча "
        "проглатываться общим 'Xray could not be resynced'"
    )
    assert any("move the A record" in m for m in messages), (
        "единственная операторская инструкция обязана выйти всегда, независимо от того, чем "
        "закончился локальный синк с Xray"
    )


def test_a_failed_restart_is_retried_next_tick_without_recontacting_the_master(app, db, monkeypatch):
    from panel_core.jobs import transfer as job
    from panel_core.services.master_client import CLAIMED_SETTING_KEY

    monkeypatch.setenv("NODE_TRANSFER_TOKEN", _token())

    with (
        patch.object(job, "MasterClient") as client_cls,
        patch("panel_core.xray.facade.generate_config_file"),
        patch(
            "panel_core.xray.facade.restart_xray_container",
            side_effect=[RuntimeError("docker down"), None],
        ) as mock_restart,
    ):
        client_cls.return_value.claim.return_value = _REPLY
        job.claim_state_job()

        assert mock_restart.call_count == 1
        assert db.session.get(SystemSetting, job.RESYNC_PENDING_SETTING_KEY).value == "1"

        job.claim_state_job()

    assert mock_restart.call_count == 2, (
        "node_transfer_claimed уже стоит — задача не имеет права молча выйти по guard'у, пока "
        "локальный Xray не синхронизирован"
    )
    assert client_cls.return_value.claim.call_count == 1, (
        "повтор синка чинит только локальный Xray; данные уже забраны, второй заявки мастеру быть не должно"
    )
    assert db.session.get(SystemSetting, job.RESYNC_PENDING_SETTING_KEY).value == "0"
    assert db.session.get(SystemSetting, CLAIMED_SETTING_KEY).value == "1"


def test_a_secret_path_mismatch_is_logged(app, db, monkeypatch, caplog):
    from panel_core.jobs import transfer as job

    monkeypatch.setenv("NODE_TRANSFER_TOKEN", _token())
    monkeypatch.setenv("PANEL_DOMAIN", "alpha.example.com")
    monkeypatch.setenv("PROXY_DOMAIN", "www.google.com")
    monkeypatch.setenv("PANEL_SECRET_PATH", "brand-new-random-path")

    reply = dict(_REPLY)
    reply["cold"] = dict(_REPLY["cold"])
    reply["cold"]["identity"] = {
        "panel_domain": "alpha.example.com",
        "proxy_domain": "www.google.com",
        "secret_path": "old-secret-path",
    }

    with (
        patch.object(job, "MasterClient") as client_cls,
        caplog.at_level("ERROR", logger="panel_core.jobs.transfer"),
    ):
        client_cls.return_value.claim.return_value = reply
        job.claim_state_job()

    messages = [rec.message for rec in caplog.records]
    assert any("old-secret-path" in m for m in messages), (
        "рассинхрон PANEL_SECRET_PATH с личностью умершей ноды обязан попасть в лог — иначе опрос "
        "мастера будет молча получать 404 (грабля 2026-08-23), а оператор не узнает почему"
    )


def test_a_failed_apply_state_does_not_leave_a_live_looking_empty_node(app, db, monkeypatch):
    from panel_core.jobs import transfer as job
    from panel_core.services.master_client import CLAIMED_SETTING_KEY

    monkeypatch.setenv("NODE_TRANSFER_TOKEN", _token())

    with (
        patch.object(job, "MasterClient") as client_cls,
        patch.object(job, "apply_state", side_effect=RuntimeError("malformed reply")),
    ):
        client_cls.return_value.claim.return_value = _REPLY
        with pytest.raises(RuntimeError):
            job.claim_state_job()

    cfg = db.session.get(FederationConfig, 1)
    assert cfg is None or not cfg.federation_token, (
        "провалившийся apply_state не должен оставлять рабочий federation-токен: с ним мастер, "
        "дотянувшись до этой ноды, получит 200 вместо 401, поверит, что перенос завершён, "
        "погасит transfer_state и перепишет хорошее зеркало пустым состоянием этой ноды"
    )
    assert db.session.get(SystemSetting, CLAIMED_SETTING_KEY) is None


def test_the_resync_flag_is_armed_before_the_first_resync_attempt(app, db, monkeypatch):
    from panel_core.jobs import transfer as job

    monkeypatch.setenv("NODE_TRANSFER_TOKEN", _token())

    def _crash(master_url):
        assert job._get_setting(job.RESYNC_PENDING_SETTING_KEY) == "1", (
            "смерть процесса прямо здесь не должна оставить нетронутым флаг ожидания синка — "
            "иначе нода 'забрала всё', но ветка ретрая на следующем тике никогда не сработает, "
            "и конфиг Xray не сгенерируется никогда"
        )
        raise RuntimeError("process died right here")

    with (
        patch.object(job, "MasterClient") as client_cls,
        patch.object(job, "_resync_xray", side_effect=_crash),
    ):
        client_cls.return_value.claim.return_value = _REPLY
        with pytest.raises(RuntimeError):
            job.claim_state_job()
