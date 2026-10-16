"""Offline fixtures executing real workflow shell, without release credentials.

The small readers deliberately target this workflow's indentation/expressions,
not a general YAML or Actions implementation, so CI needs no extra dependency.
"""
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

WORKFLOW = (Path(__file__).parents[1] / '.github/workflows/reusable-accepted-merge-release.yml').read_text()


def step(name):
    return WORKFLOW.split('      - name: ' + name + '\n', 1)[1].split('\n      - ', 1)[0]


def shell(name):
    block = step(name).split('        run: |\n', 1)[1]
    return '\n'.join(line[10:] for line in block.splitlines() if line.startswith('          '))


def output_expression(job, key):
    block = WORKFLOW.split('\n  ' + job + ':\n', 1)[1]
    block = block.split('\n    outputs:\n', 1)[1].split('\n    steps:', 1)[0]
    return re.search(r'^      ' + re.escape(key) + r': \$\{\{ (.*?) \}\}$', block, re.M).group(1)


def fallback(expression, context):
    # All tested output expressions are a simple ordered fallback chain.
    for token in expression.split(' || '):
        value = token[1:-1] if token.startswith("'") else context.get(token, '')
        if value:
            return value
    return ''


class WorkflowOutcome(unittest.TestCase):
    def test_all_six_states_are_exposed_from_release_job(self):
        expression = output_expression('release', 'state')
        for state, source in [('published', 'finalize'), ('publishing', 'checkpoint'),
                              ('build-only', 'shortcircuit'), ('held', 'shortcircuit'),
                              ('skipped', 'shortcircuit'), ('failed', 'missing')]:
            with self.subTest(state=state):
                self.assertEqual(fallback(expression, {f'steps.{source}.outputs.state': state}), state)
        self.assertEqual(fallback(expression, {}), 'failed')

    def test_normalization_failure_exposes_failed(self):
        self.assertIn('value: ${{ jobs.outcome.outputs.state }}', WORKFLOW)
        outcome = WORKFLOW.split('\n  outcome:\n', 1)[1]
        self.assertIn('if: ${{ always() }}', outcome)
        self.assertIn('needs: [normalize, release]', outcome)
        self.assertEqual(fallback(output_expression('outcome', 'state'), {}), 'failed')

    def test_published_supersedes_checkpoint(self):
        self.assertEqual(fallback(output_expression('release', 'state'), {
            'steps.finalize.outputs.state': 'published',
            'steps.checkpoint.outputs.state': 'publishing'}), 'published')

    def test_checkpoint_is_after_release_identity_and_before_modrinth(self):
        self.assertLess(WORKFLOW.index('id: created_release'), WORKFLOW.index('id: checkpoint'))
        self.assertLess(WORKFLOW.index('id: checkpoint'), WORKFLOW.index('id: publish_modrinth'))
        self.assertIn("if: steps.decide.outputs.state == 'publishing'", step('Record GitHub publication checkpoint'))

    def test_checkpoint_shell_preserves_identity_on_later_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'output'
            subprocess.run(['bash', '-e', '-c', shell('Record GitHub publication checkpoint')],
                           env={**os.environ, 'GITHUB_OUTPUT': str(output), 'RELEASE_ID': '123',
                                'RELEASE_URL': 'https://example.invalid/release/123', 'MODRINTH_ID': ''}, check=True)
            values = dict(line.split('=', 1) for line in output.read_text().splitlines())
            context = {f'steps.checkpoint.outputs.{key}': value for key, value in values.items()}
            self.assertEqual(fallback(output_expression('release', 'state'), context), 'publishing')
            self.assertEqual(fallback(output_expression('release', 'release-id'), context), '123')
            self.assertEqual(fallback(output_expression('release', 'release-url'), context), values['release-url'])

    def test_dry_and_held_calls_do_not_probe_external_services(self):
        for name in ['Look up existing release tag', 'Look up existing GitHub Release object',
                     'Look up existing Modrinth version']:
            self.assertIn('if: inputs.authorized && !inputs.hold', step(name))


class GitHubLookup(unittest.TestCase):
    def run_lookup(self, status, exit_code, body=''):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            gh = root / 'gh'
            gh.write_text('#!/bin/sh\nprintf "%s" "$FIXTURE_RESPONSE"\nexit "$FIXTURE_EXIT"\n')
            gh.chmod(0o755)
            response = '' if status is None else f'HTTP/2.0 {status} Fixture\r\nContent-Type: application/json\r\n\r\n{body}\n'
            output = root / 'output'
            result = subprocess.run(['bash', '-e', '-o', 'pipefail', '-c', shell('Look up existing GitHub Release object')],
                                    env={**os.environ, 'PATH': str(root) + ':' + os.environ['PATH'],
                                         'RUNNER_TEMP': directory, 'GITHUB_OUTPUT': str(output),
                                         'GITHUB_REPOSITORY': 'fixture/fixture', 'VERSION': '1.2.3',
                                         'FIXTURE_RESPONSE': response, 'FIXTURE_EXIT': str(exit_code)},
                                    capture_output=True, text=True)
            return result, output.read_text() if output.exists() else ''

    def test_existing_release_is_reused(self):
        result, output = self.run_lookup(200, 0, '{"id":123,"html_url":"https://example.invalid/release/123"}')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('exists=true\nid=123\nurl=https://example.invalid/release/123', output)

    def test_only_not_found_is_absent(self):
        result, output = self.run_lookup(404, 1, '{"message":"Not Found"}')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(output, 'exists=false\n')

    def test_operational_errors_fail_closed(self):
        for status in [401, 403, 429, 500, None]:
            with self.subTest(status=status):
                result, output = self.run_lookup(status, 1)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn('exists=false', output)
                self.assertIn('refusing to assume', result.stdout)

    def test_malformed_success_fails_closed(self):
        result, output = self.run_lookup(200, 0, '{}')
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('exists=false', output)


if __name__ == '__main__':
    unittest.main()
