"""CB-21: task text accepted by the API follows the existing Task-description rules."""
import unittest
from unittest.mock import patch

from pydantic import ValidationError

from app import main
from app.main import CreateTaskRequest, RedirectAgentRequest, ReplyAgentRequest, StartAgentRequest
from app.task_domain import TASK_DESCRIPTION_LIMIT
from test_api_security import call, headers

LIMIT = TASK_DESCRIPTION_LIMIT


class StartAgentTaskTests(unittest.TestCase):
    def test_blank_or_overlong_task_is_rejected(self):
        for value in ('', ' ', '\n\t ', 'x' * (LIMIT + 1), ' ' + 'x' * (LIMIT + 1) + ' '):
            with self.subTest(length=len(value)), self.assertRaises(ValidationError):
                StartAgentRequest(task=value)

    def test_task_uses_task_description_normalization(self):
        self.assertEqual(StartAgentRequest(task='  Review\n  the module \n').task, 'Review\n  the module')
        self.assertEqual(StartAgentRequest(task='x' * LIMIT).task, 'x' * LIMIT)
        self.assertEqual(StartAgentRequest(task='  Review  ').task, CreateTaskRequest(title='t', description='  Review  ').description)

    def test_api_rejects_invalid_task_before_starting_anything(self):
        for path in ('/agents/start', '/agents/some-parent/children/start'):
            for task in ('   ', 'x' * (LIMIT + 1)):
                with self.subTest(path=path, length=len(task)), \
                     patch.object(main, 'start_agent', side_effect=AssertionError('must not start')) as start:
                    body = ('{"task": "%s"}' % task).encode()
                    status, _, _ = call('POST', path, headers(content_type='application/json'), body)
                    self.assertEqual(status, 422)
                    start.assert_not_called()

    def test_api_passes_normalized_task(self):
        with patch.object(main, 'start_agent', return_value='agent-id') as start:
            status, _, _ = call('POST', '/agents/start', headers(content_type='application/json'),
                                b'{"task": "  Write tests  "}')
        self.assertEqual(status, 200)
        self.assertEqual(start.call_args.args[0], 'Write tests')


class ContinuationTextTests(unittest.TestCase):
    """Reply answers and Redirect instructions: non-blank and within the description limit, sent verbatim."""
    def test_blank_or_overlong_text_is_rejected(self):
        for model, field in ((ReplyAgentRequest, 'answer'), (RedirectAgentRequest, 'instruction')):
            for value in ('', ' \n', 'x' * (LIMIT + 1)):
                with self.subTest(model=model.__name__, length=len(value)), self.assertRaises(ValidationError):
                    model(**{field: value})

    def test_valid_text_is_preserved_exactly(self):
        text = '  Inspect auth "please" & echo literal\nOnly auth.  '
        self.assertEqual(ReplyAgentRequest(answer=text).answer, text)
        self.assertEqual(RedirectAgentRequest(instruction=text).instruction, text)
        self.assertEqual(ReplyAgentRequest(answer='x' * LIMIT).answer, 'x' * LIMIT)


if __name__ == '__main__':
    unittest.main()
