"""The host entrypoint accepts only the small Airflow command protocol."""

import base64
import importlib.util
from pathlib import Path
import unittest

SOURCE = Path(__file__).resolve().parents[1] / "deployment" / "nfl" / "ssh_entrypoint.py"
SPEC = importlib.util.spec_from_file_location("nfl_ssh_entrypoint", SOURCE)
ENTRYPOINT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ENTRYPOINT)


def encode(value):
    return base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii")


class CommandArgumentsTests(unittest.TestCase):
    def test_check(self):
        self.assertEqual(ENTRYPOINT.command_arguments("check"), ["--check"])

    def test_airflow_run_id(self):
        run_id = "scheduled__2026-09-10T04:00:00+00:00"
        for encoded in (encode(run_id), encode(run_id).rstrip("=")):
            self.assertEqual(ENTRYPOINT.command_arguments("refresh " + encoded), ["--run-id=" + run_id])

    def test_unicode(self):
        self.assertEqual(ENTRYPOINT.command_arguments("refresh " + encode("manual__é")), ["--run-id=manual__é"])

    def test_shell_text_is_only_an_argument(self):
        run_id = "--help; $(id) `whoami`"
        self.assertEqual(ENTRYPOINT.command_arguments("refresh " + encode(run_id)), ["--run-id=" + run_id])

    def test_rejects_other_commands_and_extra_arguments(self):
        for command in ("", "bash", "check ", "check; id", "refresh", "refresh abc def", "refresh  abc", "refresh YQ===", "refresh ++++"):
            with self.subTest(command=command), self.assertRaises(ValueError):
                ENTRYPOINT.command_arguments(command)

    def test_rejects_invalid_decoded_data(self):
        for value in ("", " ", "\n", "a\x00b", "a\x7fb", "a\x85b", "a" * 513, "é" * 257):
            with self.subTest(length=len(value)), self.assertRaises(ValueError):
                ENTRYPOINT.command_arguments("refresh " + encode(value))
        with self.assertRaises(ValueError):
            ENTRYPOINT.command_arguments("refresh _w==")

    def test_rejects_noncanonical_padding_bits(self):
        with self.assertRaises(ValueError):
            ENTRYPOINT.command_arguments("refresh YR==")

    def test_accepts_maximum_run_id(self):
        run_id = "a" * 512
        self.assertEqual(ENTRYPOINT.command_arguments("refresh " + encode(run_id)), ["--run-id=" + run_id])


if __name__ == "__main__":
    unittest.main()
