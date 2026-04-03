import { build, context } from "esbuild";

const isWatch = process.argv.includes("--watch");

// Plugin to stub out optional peer deps that Ink imports but doesn't need at runtime
const stubPlugin = {
  name: "stub-optional",
  setup(build) {
    const stubs = ["react-devtools-core", "yoga-wasm-web"];
    for (const mod of stubs) {
      build.onResolve({ filter: new RegExp(`^${mod}$`) }, () => ({
        path: mod,
        namespace: "stub",
      }));
    }
    build.onLoad({ filter: /.*/, namespace: "stub" }, () => ({
      contents: "export default undefined;",
      loader: "js",
    }));
  },
};

const options = {
  entryPoints: ["src/cli.tsx"],
  bundle: true,
  platform: "node",
  target: "node18",
  format: "esm",
  outfile: "dist/cli.js",
  sourcemap: false,
  minify: !isWatch,
  banner: {
    js: '#!/usr/bin/env node\nimport { createRequire } from "module"; const require = createRequire(import.meta.url);',
  },
  plugins: [stubPlugin],
  external: [
    // Node builtins only
    "node:*",
    "child_process",
    "fs",
    "path",
    "os",
    "readline",
    "stream",
    "util",
    "crypto",
    "events",
    "net",
    "tls",
    "http",
    "https",
    "url",
    "zlib",
    "buffer",
    "string_decoder",
    "assert",
    "process",
  ],
  define: {
    "process.env.NODE_ENV": isWatch ? '"development"' : '"production"',
  },
};

if (isWatch) {
  const ctx = await context(options);
  await ctx.watch();
  console.log("Watching for changes...");
} else {
  await build(options);
  console.log("Built dist/cli.js");
}
