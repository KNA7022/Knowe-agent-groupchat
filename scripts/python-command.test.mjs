import assert from 'node:assert/strict';
import test from 'node:test';
import { parsePythonCommand } from './python-command.mjs';

test('keeps an explicit Python executable path with spaces intact', () => {
  assert.deepEqual(
    parsePythonCommand('C:\\Program Files\\Python312\\python.exe'),
    { command: 'C:\\Program Files\\Python312\\python.exe', args: [] },
  );
});

test('splits only the supported Windows launcher command', () => {
  assert.deepEqual(parsePythonCommand('py -3.12'), { command: 'py', args: ['-3.12'] });
});
