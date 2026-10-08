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
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM responses").fetchone()[0], 1
                )
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM responses_v2").fetchone()[0], 1
                )
                self.assertEqual(server.list_subjects(conn, owner)[0]["name"], "Тест")
                self.assertEqual(server.list_subjects_v2(conn, owner)[0]["name"], "Тест")
        finally:
            server.DB_PATH = old_path
            if os.path.exists(path):
                os.unlink(path)


if __name__ == "__main__":
    unittest.main()
