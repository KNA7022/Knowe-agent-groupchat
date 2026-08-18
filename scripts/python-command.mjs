/** Parse known launcher commands without breaking an explicit executable path. */
export function parsePythonCommand(executable) {
  if (executable === 'py -3.12') return { command: 'py', args: ['-3.12'] };
  return { command: executable, args: [] };
}
