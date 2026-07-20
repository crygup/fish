from core.bot import required_intents


def test_gateway_intents_are_explicitly_scoped() -> None:
    intents = required_intents()
    assert intents.message_content
    assert intents.members
    assert intents.presences
    assert intents.moderation
    assert not intents.voice_states
    assert not intents.integrations
    assert not intents.guild_scheduled_events
