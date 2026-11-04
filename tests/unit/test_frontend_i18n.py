"""Frontend static hygiene: Korean i18n completeness and zero hardcoded/internal wording."""

import pathlib
import re
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
WEB_ROOT = REPO_ROOT / "src" / "web"
JS_ROOT = WEB_ROOT / "assets" / "js"
KO_PATH = JS_ROOT / "i18n" / "ko.js"


def _unescape_js(raw: str) -> str:
    s = re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), raw)
    s = s.replace(r"\n", "\n")
    s = re.sub(r'\\([\'\"\\])', r"\1", s)
    return s


def _extract_ko_keys(src: str):
    keys = []
    key_re = re.compile(r"^\s*(['\"])((?:\\.|(?!\1)[^\n])*)\1\s*:", re.MULTILINE)
    for m in key_re.finditer(src):
        keys.append(_unescape_js(m.group(2)))
    return keys


class FrontendHygieneTest(unittest.TestCase):
    def test_static_telemetry_catalog_removed(self):
        self.assertFalse(
            (REPO_ROOT / "src" / "controlplane" / "services" / "telemetry_catalog.py").exists(),
            "telemetry_catalog.py must remain removed in favor of dynamic telemetry profiling",
        )

    def test_no_internal_or_hardcoded_strings_in_web(self):
        banned = (
            "젯스키",
            "Jetski",
            "jetski",
            "telemetry_catalog",
            "04:00 KST",
            "krwPerMillion",
            "MAX_LOG_READ",
            "USD/KRW",
            "dailyHourLocal",
            "Baseline Reference",
        )
        for path in sorted(WEB_ROOT.rglob("*")):
            if not path.is_file() or path.suffix not in {".js", ".html", ".css"}:
                continue
            text = path.read_text(encoding="utf-8")
            for term in banned:
                self.assertNotIn(term, text, f"Banned hardcoded/internal string {term!r} found in {path}")

    def test_korean_dictionary_has_no_duplicates_and_covers_all_ui_strings(self):
        ko_src = KO_PATH.read_text(encoding="utf-8")
        keys = _extract_ko_keys(ko_src)
        seen = set()
        dups = []
        for k in keys:
            if k in seen:
                dups.append(k)
            seen.add(k)
        self.assertEqual(dups, [], f"Duplicate keys in ko.js: {dups[:10]}")

        call_re = re.compile(r"\bt\(\s*(['\"])((?:\\.|(?!\1)[^\n])*)\1")
        missing = []
        for js_file in sorted(JS_ROOT.rglob("*.js")):
            if js_file == KO_PATH:
                continue
            src = js_file.read_text(encoding="utf-8")
            for m in call_re.finditer(src):
                key = _unescape_js(m.group(2))
                if key not in seen:
                    missing.append((js_file.name, key))
        self.assertEqual(missing, [], f"Missing Korean translations in ko.js: {missing[:15]}")


if __name__ == "__main__":
    unittest.main()
