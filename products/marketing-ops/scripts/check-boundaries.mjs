import { readdir, readFile } from "node:fs/promises";
import { extname, join, relative } from "node:path";
import { fileURLToPath } from "node:url";

const productRoot = fileURLToPath(new URL("..", import.meta.url));
const sourceExtensions = new Set([".ts", ".tsx", ".mts", ".cts", ".js", ".jsx"]);
const prohibitedPatterns = [
  /from\s+["'][^"']*(?:^|\/)apps\/(?:api|web)(?:\/|["'])/,
  /from\s+["'][^"']*@soa\//,
  /from\s+["'][^"']*sales[-_]order/i,
];

async function walk(directory) {
  const entries = await readdir(directory, { withFileTypes: true });
  const files = [];

  for (const entry of entries) {
    if (["node_modules", ".next", ".turbo", "dist", "coverage"].includes(entry.name)) {
      continue;
    }

    const path = join(directory, entry.name);
    if (entry.isDirectory()) {
      files.push(...(await walk(path)));
    } else if (sourceExtensions.has(extname(entry.name))) {
      files.push(path);
    }
  }

  return files;
}

const violations = [];
for (const file of await walk(productRoot)) {
  const content = await readFile(file, "utf8");
  for (const pattern of prohibitedPatterns) {
    if (pattern.test(content)) {
      violations.push(`${relative(productRoot, file)} matched ${pattern}`);
    }
  }
}

if (violations.length > 0) {
  console.error("Product-boundary violations detected:\n" + violations.join("\n"));
  process.exit(1);
}

console.log("Product-boundary check passed.");
