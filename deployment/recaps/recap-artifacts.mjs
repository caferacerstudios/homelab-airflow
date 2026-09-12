import fs from "node:fs";
import path from "node:path";
import { randomUUID } from "node:crypto";

const object = (value) => value !== null && typeof value === "object" && !Array.isArray(value);
const nonempty = (value) => typeof value === "string" && value.trim().length > 0;

// Existing editorial content can predate the current model schema. Keep valid
// prose without requiring newer metadata or exactly three historical bullets.
export function isCompleteRecap(recap) {
  return object(recap)
    && Array.isArray(recap.segments) && recap.segments.length > 0
    && recap.segments.every((segment) => object(segment) && ["text", "player"].includes(segment.t) && typeof segment.v === "string")
    && nonempty(recap.segments.map((segment) => segment.v).join(""))
    && Array.isArray(recap.bullets) && recap.bullets.length > 0
    && recap.bullets.every(nonempty);
}

export function validateGeneratedRecap(recap) {
  if (!isCompleteRecap(recap) || recap.bullets.length !== 3) throw new Error("Invalid generated recap segments or highlights");
  for (const segment of recap.segments) {
    if (Object.keys(segment).length !== 4 || !["t", "v", "id", "name"].every((key) => Object.hasOwn(segment, key))) throw new Error("Invalid generated recap segment schema");
    if (segment.t === "text" && (segment.id !== null || segment.name !== null)) throw new Error("Invalid generated text segment");
    if (segment.t === "player" && (!(typeof segment.id === "string" ? nonempty(segment.id) : Number.isSafeInteger(segment.id)) || !nonempty(segment.name) || !nonempty(segment.v))) throw new Error("Invalid generated player segment");
  }
}

export function atomicWriteJson(filename, value) {
  fs.mkdirSync(path.dirname(filename), { recursive: true });
  const temporary = `${filename}.write-${randomUUID()}`;
  try {
    fs.writeFileSync(temporary, `${JSON.stringify(value, null, 2)}\n`, { flag: "wx", mode: 0o644 });
    fs.renameSync(temporary, filename);
  } finally { fs.rmSync(temporary, { force: true }); }
}
