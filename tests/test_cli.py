import unittest

from work_orchestrator.cli import parser


class CliWorkspaceRestoreTests(unittest.TestCase):
    def test_restore_accepts_single_project_filter(self):
        args = parser().parse_args(["workspace", "restore", "default", "--project", "utm7", "--visual", "terminator", "--dry-run"])
        self.assertEqual(args.project, "utm7")
        self.assertTrue(args.dry_run)
        self.assertEqual(args.visual, "terminator")

    def test_restore_keeps_ambiguous_project_value_as_one_literal(self):
        args = parser().parse_args(["workspace", "restore", "--project", "utm7,bluepexvpn"])
        self.assertEqual(args.project, "utm7,bluepexvpn")

    def test_visuals_migration_arguments(self):
        args = parser().parse_args(["visuals", "migrate-defaults", "--project", "utm7", "--dry-run"])
        self.assertEqual(args.visuals_command, "migrate-defaults")
        self.assertEqual(args.project, "utm7")
        self.assertTrue(args.dry_run)


if __name__ == "__main__":
    unittest.main()
