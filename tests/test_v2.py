import math
import os
import tempfile
import unittest

import server


class V2ScoringTests(unittest.TestCase):
    def test_profile_reference_points(self):
        cases = (
            ([2] * 40, (0.0, 0.0, "I")),
            ([4] * 40, (40.0, 40.0, "I")),
            ([0] * 40, (-40.0, -40.0, "III")),
        )
        for answers, expected in cases:
            _, ya, my, quadrant = server.compute_scores_v2(answers)
            self.assertEqual((ya, my, quadrant), expected)

    def test_link_polar_mapping(self):
        self.assertEqual(server._link_cell_v2(2, 4), 0j)
        self.assertAlmostEqual(server._link_cell_v2(4, 0).real, 2)
        self.assertAlmostEqual(server._link_cell_v2(4, 4).imag, 2)
        self.assertAlmostEqual(server._link_cell_v2(0, 0).real, -2)
        diagonal = server._link_cell_v2(4, 2)
        self.assertAlmostEqual(diagonal.real, 2 / math.sqrt(2))
        self.assertAlmostEqual(diagonal.imag, diagonal.real)

    def test_leadership_reference_cases(self):
        zero = [[0j for _ in range(3)] for _ in range(3)]
        strong = [[2 + 0j for _ in range(3)] for _ in range(3)]
        one_cell = [[0j for _ in range(3)] for _ in range(3)]
        one_cell[0][0] = 2 + 0j

        inactive = server.leadership_summary_v2(zero, zero)
        self.assertEqual(inactive["classification"], "inactive")
        self.assertEqual(inactive["index"], 0)
        self.assertIsNone(inactive["leader_direction"])

        balanced = server.leadership_summary_v2(strong, strong)
        self.assertEqual(balanced["classification"], "balanced")
        self.assertEqual(balanced["out"]["strength"], 1)
        self.assertEqual(balanced["in"]["strength"], 1)

        system = server.leadership_summary_v2(strong, zero)
        self.assertEqual(system["classification"], "system")
        self.assertEqual(system["index"], 1)
        self.assertEqual(system["leader_direction"], "out")
        self.assertEqual(system["out"]["coverage"], 1)

        local = server.leadership_summary_v2(one_cell, zero)
        self.assertEqual(local["classification"], "local")
        self.assertEqual(local["index"], 1)
        self.assertAlmostEqual(local["out"]["strength"], 1 / 9, places=4)
        self.assertAlmostEqual(local["out"]["coverage"], 1 / 9, places=4)

    def test_leadership_keeps_channel_and_delay_information(self):
        matrix = [[0j for _ in range(3)] for _ in range(3)]
        matrix[2][1] = server._link_cell_v2(4, 2)
        summary = server.leadership_summary_v2(
            matrix, [[0j for _ in range(3)] for _ in range(3)]
        )
        self.assertEqual(summary["out"]["delay"], 45)
        self.assertAlmostEqual(summary["out"]["source"]["goal"], 1 / 3, places=4)
        self.assertAlmostEqual(summary["out"]["target"]["resource"], 1 / 3, places=4)

    def test_v1_and_v2_storage_are_independent(self):
        old_path = server.DB_PATH
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.unlink(path)
        server.DB_PATH = path
        try:
            server.init_db()
            owner = server.visitor_hash("test-browser-token")
            with server.connect() as conn:
                server.save_response(
                    conn,
                    {"mode": "self", "subject": "Тест", "answers": [2] * 40},
                    owner,
                )
                server.save_response_v2(
                    conn,
                    {"mode": "self", "subject": "Тест", "answers": [2] * 40},
                    owner,
                )
                saved_link = server.save_link_v2(
                    conn,
                    {
                        "context": "family",
                        "self": "Лидер",
                        "partner": "Ведомый",
                        "answers": [4, 0] * 9 + [2, 0] * 9,
                    },
                    owner,
                )
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM responses").fetchone()[0], 1
                )
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM responses_v2").fetchone()[0], 1
                )
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM link_responses_v2").fetchone()[0], 1
                )
                self.assertEqual(
                    saved_link["link"]["leadership"]["classification"], "system"
                )
                self.assertEqual(server.list_subjects(conn, owner)[0]["name"], "Тест")
                self.assertEqual(server.list_subjects_v2(conn, owner)[0]["name"], "Тест")
        finally:
            server.DB_PATH = old_path
            if os.path.exists(path):
                os.unlink(path)


if __name__ == "__main__":
    unittest.main()
