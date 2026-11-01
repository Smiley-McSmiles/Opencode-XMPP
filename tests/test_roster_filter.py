"""quiet_unknown_senders means "not a roster contact", not "not an owner"."""

from types import MethodType, SimpleNamespace

from opencode_xmpp import bot as botmod


class FakeRoster:
    def __init__(self, items):
        self.items = items

    def keys(self):
        return self.items.keys()

    def __getitem__(self, key):
        return self.items[key]


def _bridge(quiet=True, items=None, loaded=True):
    fake = SimpleNamespace(
        cfg=SimpleNamespace(
            policy=SimpleNamespace(quiet_unknown_senders=quiet,
                                   owners=("owner@example.org",)),
        ),
        client_roster=FakeRoster(items or {}),
        _roster_loaded=loaded,
    )
    for name in ("_is_contact", "_should_quiet"):
        setattr(fake, name, MethodType(getattr(botmod.Bridge, name), fake))
    return fake


def test_owners_and_roster_contacts_are_answered():
    fake = _bridge(items={
        "friend@example.org": {"subscription": "both"},
        "half@example.org": {"subscription": "from"},
        "requested@example.org": {"subscription": "to"},
        "pending@example.org": {"subscription": "none"},
    })

    assert fake._should_quiet("owner@example.org") is False
    assert fake._should_quiet("friend@example.org") is False
    assert fake._should_quiet("friend@example.org/phone") is False
    assert fake._should_quiet("half@example.org") is False
    assert fake._should_quiet("requested@example.org") is False
    # In the roster with no subscription yet (an invite they have not accepted
    # is still the operator's own contact): answer them.
    assert fake._should_quiet("pending@example.org") is False
    assert fake._is_contact("pending@example.org") is True
    assert fake._should_quiet("stranger@example.org") is True


def test_quiet_off_answers_every_guest():
    fake = _bridge(quiet=False)
    assert fake._should_quiet("stranger@example.org") is False


def test_a_missing_roster_never_silences_the_bot():
    """If the roster fetch failed, "not in the roster" proves nothing."""
    fake = _bridge(items={}, loaded=False)
    assert fake._should_quiet("stranger@example.org") is False


def test_an_empty_roster_that_loaded_still_quiets_strangers():
    fake = _bridge(items={}, loaded=True)
    assert fake._should_quiet("stranger@example.org") is True
