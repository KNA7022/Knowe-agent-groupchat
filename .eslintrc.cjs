/** Baseline parser gate; rules are enabled incrementally as legacy findings are repaired. */
module.exports = {
  root: true,
  env: { browser: true, node: true, es2022: true },
  parser: '@typescript-eslint/parser',
  parserOptions: {
    ecmaVersion: 'latest',
    sourceType: 'module',
    ecmaFeatures: { jsx: true },
  },
  plugins: ['@typescript-eslint', 'react-hooks'],
  ignorePatterns: ['out/', 'dist/', 'release/', 'node_modules/'],
  rules: {},
};
