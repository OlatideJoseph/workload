from django.test import SimpleTestCase

from hictx.graph.services import references


class FixReferenceTests(SimpleTestCase):
    def test_matches_closing_keywords(self):
        for text in ["Fixes #91", "fixed #91", "Closes #91", "resolves #91"]:
            self.assertEqual(references.find_fix_references(text), ["91"], text)

    def test_matches_gh_prefix(self):
        self.assertEqual(references.find_fix_references("fixes GH-42"), ["42"])

    def test_ignores_bare_mentions(self):
        self.assertEqual(references.find_fix_references("see #91 for context"), [])

    def test_multiple_and_deduped(self):
        self.assertEqual(
            references.find_fix_references("fixes #1, closes #2, fixes #1"),
            ["1", "2"],
        )

    def test_empty(self):
        self.assertEqual(references.find_fix_references(""), [])


class IssueReferenceTests(SimpleTestCase):
    def test_tags_fix_vs_bare(self):
        refs = references.find_issue_references("fixes #91; see also #12")
        by_num = {r.number: r.is_fix for r in refs}
        self.assertTrue(by_num["91"])
        self.assertFalse(by_num["12"])


class AdrReferenceTests(SimpleTestCase):
    def test_normalises_forms(self):
        self.assertEqual(references.find_adr_references("implements ADR-7"), ["ADR-7"])
        self.assertEqual(references.find_adr_references("per adr 7"), ["ADR-7"])

    def test_strips_leading_zeros(self):
        # korifi refs "ADR-004"; the ingested key is "ADR-4" — they must match.
        self.assertEqual(references.find_adr_references("see ADR-004"), ["ADR-4"])

    def test_filename_and_punctuation_forms(self):
        # The common "ADR `0019-name.md`" filename reference, plus ADR: / ADR #.
        self.assertEqual(
            references.find_adr_references("Document in ADR `0019-service.md`"),
            ["ADR-19"],
        )
        self.assertEqual(references.find_adr_references("ADR: 4 and ADR #16"),
                         ["ADR-4", "ADR-16"])

    def test_prose_mentions_yield_no_number(self):
        # "write an ADR", "ADRs" must NOT produce a spurious number.
        self.assertEqual(references.find_adr_references("write an ADR about retries"), [])
        self.assertEqual(references.find_adr_references("see the ADRs folder"), [])

    def test_none(self):
        self.assertEqual(references.find_adr_references("no decisions here"), [])
