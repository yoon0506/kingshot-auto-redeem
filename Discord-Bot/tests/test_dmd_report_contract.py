"""Report safety-state contract checks without importing Discord or starting the bot."""
import ast
import unittest
from pathlib import Path


BOT = Path(__file__).resolve().parents[1] / 'bot.py'


class ReportSafetyContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = BOT.read_text(encoding='utf-8')
        cls.ast = ast.parse(cls.source, filename=str(BOT))
        cls.bot_class = next(
            n for n in cls.ast.body if isinstance(n, ast.ClassDef) and n.name == 'KingshotBot'
        )
        cls.methods = {
            n.name: n for n in cls.bot_class.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        }

    def test_report_knows_safety_states(self):
        method = self.methods['publish_redeem_report']
        source = ast.get_source_segment(self.source, method)
        for token in ('_code_safety_state(', 'safety_signature', '"blocked"', '"cooldown"', '"probe"'):
            self.assertIn(token, source, f'Report missing expected safety integration: {token}')

    def test_report_has_correct_visible_labels(self):
        source = ast.get_source_segment(self.source, self.methods['publish_redeem_report'])
        for label in ('⛔ 자동 수령 차단', '🟠 일시 보류', '🟠 재검사 대기', '🟠 재검사 중', '🔴 만료'):
            self.assertIn(label, source)
        self.assertIn('반복된 API 오류로 자동 수령을 중단했습니다.\\n', source)

    def test_report_loop_watches_code_safety_file(self):
        source = ast.get_source_segment(self.source, self.methods['report_loop'])
        self.assertIn('_code_safety_snapshot()', source)
        self.assertIn('publish_redeem_report(code)', source)


if __name__ == '__main__':
    unittest.main()
