/// <reference types="vite/client" />

// Why this file exists: `src/preview/renderers/PdfPreview.tsx` imports
// `pdfjs-dist/build/pdf.worker.min.mjs?url`, which is a **Vite** import form rather than a
// path Node or TypeScript understands. Without `vite/client` the compiler reports
// `TS2307: Cannot find module '...pdf.worker.min.mjs?url'` — even though `pdfjs-dist` is
// installed and the worker file is present — because nothing declares the `*?url` module
// shape. That single error was the only module-resolution failure in the tree.
//
// Keeping it here rather than in `tsconfig.json`'s `types` array on purpose: this is a
// source-level reference, so it travels with the code that needs it and `tsconfig` stays
// about the build.
