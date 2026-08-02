"""§81: a purchase on top of unlimited access told the user "access until ?".

Wave 3a's §41 made `expiry_time == 0` mean "never expires" and kept it that way through a purchase --
buying a period on top of unlimited access refreshes the traffic limit and leaves the access
unlimited. What none of that reached was the message the user actually receives: the consumer's
formatter treated `0` as "no date at all" and substituted a literal question mark into
"Payment received, access until {expires}".

The bot already has wording for this and uses it on the Statistics screen: `stats.expiry.permanent`
("♾️ Бессрочно" / "♾️ Permanent"). Reusing it means no new key, so `CURRENT_BOT_TEXTS_VERSION` does
not move and no reseed is involved.

`None` still renders "?" -- an absent field is genuinely unknown, and saying "permanent" there would
be the same class of lie one step over.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

import bot_events_consumer as consumer


def _i18n():
    i18n = MagicMock()
    i18n.t = AsyncMock(side_effect=lambda key, lang, **kw: f"{key}|{kw.get('expires', '')}")
    return i18n


@pytest.mark.parametrize("etype", ["payment_succeeded", "access_granted", "access_granted_once"])
async def test_zero_reads_as_permanent_not_as_a_question_mark(etype):
    bot = AsyncMock()
    i18n = _i18n()

    await consumer._handle(
        {"type": etype, "telegram_id": 42, "payload": {"lang": "ru", "expires_at_ms": 0, "tariff_name": "Pro"}},
        lambda: bot,
        i18n,
        None,
    )

    sent = bot.send_message.await_args.kwargs.get("text") or bot.send_message.await_args.args[1]
    assert "?" not in sent, (
        f"{etype} rendered an unlimited grant as a question mark: {sent!r}. §41 keeps such an account "
        f"unlimited on purpose; the message has to say so."
    )
    assert "stats.expiry.permanent" in sent


async def test_a_missing_date_still_reads_as_unknown():
    assert await consumer._format_expires_at(None, i18n=_i18n(), lang="ru") == "?"


async def test_a_real_date_is_still_formatted_as_a_date():
    out = await consumer._format_expires_at(1_753_000_000_000, i18n=_i18n(), lang="ru")
    assert out.count(".") == 2 and ":" in out, out
