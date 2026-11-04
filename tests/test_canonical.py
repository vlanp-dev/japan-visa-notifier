from __future__ import annotations

import codecs
import json
import unittest
from pathlib import Path

from app.detection.canonical import (
    DEFAULT_EXCERPT_LIMIT,
    CanonicalSnapshot,
    CanonicalizationError,
    ContentDecodingError,
    SnapshotFormatError,
    canonicalize_page,
    decode_html,
    single_line_excerpt,
)

FIXTURES = Path(__file__).with_name("fixtures")
BASE_URL = "https://exemple.fr/calendrier"


def load_fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


class CanonicalizationTests(unittest.TestCase):
    def test_closed_fixture_is_deterministic(self) -> None:
        first = canonicalize_page(
            load_fixture("closed_page.html"), base_url=BASE_URL
        )
        second = canonicalize_page(
            load_fixture("closed_page.html"), base_url=BASE_URL
        )

        self.assertEqual(first, second)
        self.assertEqual(first.to_json(), second.to_json())
        self.assertEqual(first.digest(), second.digest())
        # The current closed state already contains these words; they are not
        # special-cased anywhere in canonicalization.
        self.assertIn("rendez-vous", first.text)
        self.assertIn("calendrier", first.text.casefold())

    def test_attribute_order_and_whitespace_are_stable(self) -> None:
        reference = canonicalize_page(
            load_fixture("closed_page.html"), base_url=BASE_URL
        )
        variant = canonicalize_page(
            load_fixture("closed_page_variant.html"), base_url=BASE_URL
        )

        self.assertEqual(reference.to_json(), variant.to_json())
        self.assertEqual(reference.digest(), variant.digest())

    def test_script_style_template_noscript_comments_and_hidden_inputs_are_dropped(
        self,
    ) -> None:
        html = """
        <html>
          <head>
            <style>STYLE_SECRET</style>
            <script>SCRIPT_SECRET</script>
          </head>
          <body>
            <p>Contenu visible</p>
            <noscript>NOSCRIPT_SECRET</noscript>
            <template><p>TEMPLATE_SECRET</p></template>
            <input type="hidden" name="secret" value="HIDDEN_SECRET">
            <!-- COMMENT_SECRET -->
          </body>
        </html>
        """
        snapshot = canonicalize_page(html)

        self.assertIn("Contenu visible", snapshot.text)
        serialized = snapshot.to_json()
        for forbidden in (
            "STYLE_SECRET",
            "SCRIPT_SECRET",
            "NOSCRIPT_SECRET",
            "TEMPLATE_SECRET",
            "HIDDEN_SECRET",
            "COMMENT_SECRET",
        ):
            self.assertNotIn(forbidden, snapshot.text)
            self.assertNotIn(forbidden, serialized)

    def test_visible_text_normalizes_unicode_and_whitespace(self) -> None:
        html = (
            "<html><body>"
            "<p>  Complet\u00a0 2026\n et\tcalendrier  </p>"
            "<p>Cafe\u0301</p>"
            "</body></html>"
        )
        snapshot = canonicalize_page(html)

        self.assertIn("Complet 2026 et calendrier", snapshot.text)
        self.assertIn("Café", snapshot.text)
        self.assertNotIn("\u00a0", snapshot.text)
        self.assertNotIn("\t", snapshot.text)

    def test_closure_candidate_is_the_nearest_block(self) -> None:
        html = """
        <div class="outer">
          <p>Annonce</p>
          <div class="inner"><span>Complet</span> <strong>2026</strong></div>
          <p>Autre information</p>
        </div>
        """
        snapshot = canonicalize_page(html, closure_anchor="Complet 2026")

        self.assertEqual(len(snapshot.closure_blocks), 1)
        candidate = snapshot.closure_blocks[0]
        self.assertEqual(candidate.tag, "div")
        self.assertEqual(candidate.excerpt, "Complet 2026")

    def test_closure_anchor_is_configurable_and_absence_yields_no_candidate(
        self,
    ) -> None:
        snapshot = canonicalize_page(
            "<div><p>Fermé jusqu'à nouvel ordre</p></div>",
            closure_anchor="fermé",
        )
        self.assertEqual(len(snapshot.closure_blocks), 1)
        self.assertEqual(
            snapshot.closure_blocks[0].excerpt, "Fermé jusqu'à nouvel ordre"
        )

        absent = canonicalize_page(
            "<p>Autre contenu</p>", closure_anchor="Introuvable"
        )
        self.assertEqual(absent.closure_blocks, ())

    def test_closure_excerpt_centers_on_a_late_anchor(self) -> None:
        filler = "contexte " * 40
        snapshot = canonicalize_page(
            f"<div><p>{filler}<strong>Complet 2026</strong> {filler}</p></div>",
            closure_anchor="Complet 2026",
        )

        self.assertEqual(len(snapshot.closure_blocks), 1)
        excerpt = snapshot.closure_blocks[0].excerpt
        self.assertIn("Complet 2026", excerpt)
        self.assertLessEqual(len(excerpt), DEFAULT_EXCERPT_LIMIT)

    def test_timestamp_candidates_use_label_and_bounded_window(self) -> None:
        snapshot = canonicalize_page(
            "<p>Dernière mise à jour : 12/03/2026 14:05</p>"
        )
        self.assertEqual(len(snapshot.timestamps), 1)
        self.assertEqual(snapshot.timestamps[0].value, "12/03/2026 14:05")
        self.assertIn("mise à jour", snapshot.timestamps[0].excerpt)

        custom = canonicalize_page(
            "<p>Ouverture prévue le 5 avril 2026</p>",
            timestamp_label="Ouverture prévue",
        )
        self.assertEqual(custom.timestamps[0].value, "5 avril 2026")

        far_date = f"<p>Mise à jour : {'x' * 200} 4 novembre 2026</p>"
        self.assertEqual(canonicalize_page(far_date).timestamps, ())
        widened = canonicalize_page(far_date, timestamp_window=300)
        self.assertEqual(widened.timestamps[0].value, "4 novembre 2026")

        no_label = canonicalize_page("<p>4 novembre 2026</p>")
        self.assertEqual(no_label.timestamps, ())

    def test_link_document_and_time_fingerprints(self) -> None:
        html = """
        <html><body>
          <a href="https://exemple.fr/a?x=1#fragment">Réserver</a>
          <a href="/documents/calendrier-2026-11-04.pdf">Calendrier (PDF)</a>
          <a href="mailto:contact@example.org">Nous écrire</a>
          <a href="javascript:alert(1)">Script</a>
          <time datetime="2026-11-04T09:30:00+01:00">4 novembre 2026</time>
        </body></html>
        """
        snapshot = canonicalize_page(html, base_url=BASE_URL)
        urls = [link.url for link in snapshot.links]

        self.assertIn("https://exemple.fr/a?x=1", urls)
        self.assertIn(
            "https://exemple.fr/documents/calendrier-2026-11-04.pdf", urls
        )
        self.assertNotIn("mailto:contact@example.org", urls)
        self.assertNotIn("javascript:alert(1)", urls)

        self.assertEqual(len(snapshot.documents), 1)
        self.assertEqual(snapshot.documents[0].date, "2026-11-04")
        self.assertEqual(
            snapshot.documents[0].url,
            "https://exemple.fr/documents/calendrier-2026-11-04.pdf",
        )

        self.assertEqual(len(snapshot.times), 1)
        self.assertEqual(
            snapshot.times[0].value, "2026-11-04T09:30:00+01:00"
        )
        self.assertEqual(snapshot.times[0].text, "4 novembre 2026")

    def test_duplicate_links_are_fingerprinted_once(self) -> None:
        snapshot = canonicalize_page(
            """
            <p><a href="https://exemple.fr/reserver">Réserver</a></p>
            <p><a href="https://exemple.fr/reserver#haut">Prendre rendez-vous</a></p>
            """
        )

        self.assertEqual(len(snapshot.links), 1)
        self.assertEqual(
            snapshot.links[0].url, "https://exemple.fr/reserver"
        )

    def test_excerpts_are_single_line_and_bounded(self) -> None:
        raw = "  première\nligne\tavec   espaces " + "x" * 200
        excerpt = single_line_excerpt(raw, limit=40)

        self.assertNotIn("\n", excerpt)
        self.assertNotIn("\t", excerpt)
        self.assertLessEqual(len(excerpt), 40)
        self.assertTrue(excerpt.endswith("…"))
        self.assertEqual(
            single_line_excerpt("  Court\n texte ", 40), "Court texte"
        )
        with self.assertRaises(ValueError):
            single_line_excerpt("texte", 0)

        long_block = "<p>Complet 2026 " + "y" * 500 + "</p>"
        snapshot = canonicalize_page(long_block)
        for candidate in snapshot.closure_blocks:
            self.assertLessEqual(len(candidate.excerpt), DEFAULT_EXCERPT_LIMIT)
            self.assertNotIn("\n", candidate.excerpt)

    def test_json_round_trip_preserves_snapshot(self) -> None:
        snapshot = canonicalize_page(
            load_fixture("closed_page.html"), base_url=BASE_URL
        )
        serialized = snapshot.to_json()

        self.assertEqual(CanonicalSnapshot.from_json(serialized), snapshot)
        self.assertEqual(
            CanonicalSnapshot.from_json(serialized.encode("utf-8")), snapshot
        )
        self.assertEqual(
            CanonicalSnapshot.from_dict(json.loads(serialized)), snapshot
        )
        self.assertEqual(
            CanonicalSnapshot.from_json(serialized).to_json(), serialized
        )

    def test_json_round_trip_rejects_malformed_snapshots(self) -> None:
        with self.assertRaises(SnapshotFormatError):
            CanonicalSnapshot.from_json("{not json")
        with self.assertRaises(SnapshotFormatError):
            CanonicalSnapshot.from_json(b"\xff\xfe\x00")

        malformed = (
            {"text": 1},
            {"text": "", "closure_blocks": "nope"},
            {"text": "", "links": [{"fingerprint": 1}]},
            {"text": "", "version": 99},
        )
        for payload in malformed:
            with self.subTest(payload=payload):
                with self.assertRaises(SnapshotFormatError):
                    CanonicalSnapshot.from_dict(payload)

    def test_decode_html_handles_bom_declared_charset_and_failures(self) -> None:
        self.assertEqual(
            decode_html(b"\xef\xbb\xbf<p>ok</p>"), "<p>ok</p>"
        )
        self.assertEqual(
            decode_html("café".encode("latin-1"), "latin-1"), "café"
        )
        self.assertEqual(
            decode_html(
                codecs.BOM_UTF16_LE + "café".encode("utf-16-le")
            ),
            "café",
        )
        self.assertEqual(decode_html("déjà"), "déjà")

        with self.assertRaises(ContentDecodingError):
            decode_html(b"\xff\x80")
        with self.assertRaises(ContentDecodingError):
            decode_html(b"texte", "jeu-de-caracteres-inconnu")

    def test_malformed_markup_does_not_crash(self) -> None:
        snapshot = canonicalize_page(
            "<html><body><p>Texte non fermé <b>gras"
        )
        self.assertIn("Texte non fermé gras", snapshot.text)

        with self.assertRaises(CanonicalizationError):
            canonicalize_page(123)  # type: ignore[arg-type]
        with self.assertRaises(CanonicalizationError):
            canonicalize_page("<p>ok</p>", timestamp_window=0)
        with self.assertRaises(CanonicalizationError):
            canonicalize_page("<p>ok</p>", excerpt_limit=0)

    def test_digest_changes_when_the_closure_announcement_changes(self) -> None:
        reference = canonicalize_page(
            load_fixture("closed_page.html"), base_url=BASE_URL
        )
        changed = canonicalize_page(
            load_fixture("closed_page.html")
            .decode("utf-8")
            .replace("Complet 2026", "Complet 2026 — plus de places"),
            base_url=BASE_URL,
        )

        self.assertNotEqual(reference.digest(), changed.digest())
        self.assertNotEqual(
            [item.fingerprint for item in reference.closure_blocks],
            [item.fingerprint for item in changed.closure_blocks],
        )


if __name__ == "__main__":
    unittest.main()
