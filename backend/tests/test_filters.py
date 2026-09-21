from app.filters import contains_reschedule_language, is_deadline_candidate, score_email


class TestScoreEmail:
    def test_matches_all_three_signals(self):
        signals = score_email(
            "Assignment 3 due 4/18",
            "Please submit assignment 3 by 4/18.",
        )
        assert signals == {"keyword": True, "action_verb": True, "date_pattern": True}

    def test_matches_no_signals(self):
        signals = score_email("Just a newsletter", "Nothing important here at all.")
        assert signals == {"keyword": False, "action_verb": False, "date_pattern": False}

    def test_rsvp_without_explicit_date_number_only_matches_keyword(self):
        """Real case verified during manual testing: 'RSVP ... by Monday'
        (no day number) only trips the keyword signal, not date_pattern —
        this is exactly the kind of miss the moderate threshold accepts.
        """
        signals = score_email(
            "RSVP for the club social",
            "Let us know by Monday if you are coming.",
        )
        assert signals["keyword"] is True
        assert signals["date_pattern"] is False


class TestIsDeadlineCandidate:
    def test_moderate_requires_two_signals(self):
        # keyword + action_verb, no clean date_pattern -> passes at moderate (>=2)
        assert is_deadline_candidate(
            "Assignment 3 due Friday",
            "Please submit your assignment by 11:59pm.",
            level="moderate",
        ) is True

    def test_single_signal_fails_moderate_but_passes_loose(self):
        subject, body = "RSVP for the club social", "Let us know by Monday if you are coming."
        assert is_deadline_candidate(subject, body, level="moderate") is False
        assert is_deadline_candidate(subject, body, level="loose") is True

    def test_strict_requires_all_three(self):
        # keyword + action_verb only, no date_pattern -> fails strict (needs 3)
        assert is_deadline_candidate(
            "Assignment due",
            "Please submit your assignment.",
            level="strict",
        ) is False

    def test_unknown_level_raises(self):
        import pytest

        with pytest.raises(ValueError):
            is_deadline_candidate("x", "y", level="nonsense")


class TestContainsRescheduleLanguage:
    def test_detects_common_reschedule_phrasing(self):
        assert contains_reschedule_language("Assignment 3 rescheduled", "") is True
        assert contains_reschedule_language("Update", "The deadline has been moved to Friday.") is True
        assert contains_reschedule_language("Update", "New due date: April 25.") is True

    def test_plain_restated_date_is_not_detected(self):
        """Known, accepted gap (docs/design-decisions.md, decision 5):
        a reschedule that doesn't use any signal words isn't caught here —
        it's caught downstream by creating a visible (not silent) second entry."""
        assert contains_reschedule_language("Assignment 3", "Assignment 3 due April 25.") is False

    def test_unrelated_email_is_not_detected(self):
        assert contains_reschedule_language("Newsletter", "Nothing about dates here.") is False
