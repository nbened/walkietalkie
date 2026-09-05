#!/usr/bin/env node
/**
 * npx callwalkietalkie
 *
 * Installs a tiny local runtime into ~/.callwalkietalkie (first run), starts the
 * chat server, and opens a page with a QR code. Scan it — phone chat opens into cmux.
 *
 * No codes to type. No git clone required. The Python server ships inside this
 * npm package; npx only caches the package, so the durable venv lives in
 * ~/.callwalkietalkie (falls back to ~/.longleash if you already have one).
 */

import { spawn, spawnSync } from "node:child_process";
import { existsSync, mkdirSync, openSync, readFileSync, writeFileSync } from "node:fs";
import { hostname, homedir, networkInterfaces, tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const BOOLEAN_FLAGS = new Set(["help", "version", "no-open", "fresh"]);
const PORT = Number(process.env.CALLWALKIETALKIE_PORT || process.env.LONGLEASH_PORT || 8787);
const TOKEN =
  process.env.CALLWALKIETALKIE_TOKEN || process.env.LONGLEASH_TOKEN || "agnostic-dispatch";
const PACKAGE_ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");
const BUNDLED_RUNTIME = join(PACKAGE_ROOT, "runtime");

function resolveHome() {
  if (process.env.CALLWALKIETALKIE_HOME) return process.env.CALLWALKIETALKIE_HOME;
  if (process.env.LONGLEASH_HOME) return process.env.LONGLEASH_HOME;
  const neu = join(homedir(), ".callwalkietalkie");
  const old = join(homedir(), ".longleash");
  if (existsSync(join(neu, "key")) || existsSync(join(neu, "venv"))) return neu;
  if (existsSync(join(old, "key")) || existsSync(join(old, "venv"))) return old;
  return neu;
}

const HOME = resolveHome();

const USAGE = `Usage: npx callwalkietalkie

Starts the chat server on this Mac and opens a page with a QR code.
First run installs a small Python runtime into ~/.callwalkietalkie.

Options:
  --port <PORT>   port for the local server (default: ${PORT})
  --fresh         kill any running server and start a new session
  --no-open       print the URL instead of opening a browser
  -h, --help
  -v, --version
`;

function fail(message) {
  console.error(`\n  ${message}\n`);
  process.exit(1);
}

function parseArgs(argv) {
  const flags = {};
  for (let i = 0; i < argv.length; i++) {
    const arg = argv[i];
    if (arg === "-h") flags.help = true;
    else if (arg === "-v") flags.version = true;
    else if (arg.startsWith("--")) {
      const [name, inline] = arg.slice(2).split("=");
      if (BOOLEAN_FLAGS.has(name) && inline === undefined) {
        flags[name] = true;
        continue;
      }
      const value = inline ?? argv[++i];
      if (value === undefined) fail(`Missing value for --${name}`);
      flags[name] = value;
    } else if (arg === "link") {
      fail("That old `link --code` flow is gone. Just run:  npx callwalkietalkie");
    }
  }
  return flags;
}

function lanAddress() {
  for (const list of Object.values(networkInterfaces())) {
    for (const net of list ?? []) {
      if (net.family === "IPv4" && !net.internal) return net.address;
    }
  }
  return null;
}

function machineName() {
  return hostname().replace(/\.local$/, "");
}

async function probe(port) {
  try {
    const res = await fetch(
      `http://127.0.0.1:${port}/setup/state?t=${encodeURIComponent(TOKEN)}`,
      { signal: AbortSignal.timeout(1500) },
    );
    if (!res.ok) return null;
    return await res.json();
  } catch {
    return null;
  }
}

/** Where the Python server files live (shipped inside the npm package). */
function runtimeDir() {
  if (existsSync(join(BUNDLED_RUNTIME, "winproxy.py"))) return BUNDLED_RUNTIME;
  // Dev fallback: package linked from the git repo, runtime not synced yet.
  const repo = join(PACKAGE_ROOT, "..");
  if (existsSync(join(repo, "winproxy.py"))) return repo;
  return null;
}

function systemPython() {
  for (const cmd of ["python3", "python"]) {
    const r = spawnSync(cmd, ["--version"], { encoding: "utf8" });
    if (r.status === 0) return cmd;
  }
  return null;
}

function run(cmd, args) {
  const r = spawnSync(cmd, args, { encoding: "utf8" });
  if (r.status !== 0) {
    const detail = (r.stderr || r.stdout || "").trim();
    fail(`\`${cmd} ${args.join(" ")}\` failed${detail ? `:\n\n${detail}` : "."}`);
  }
}

/**
 * npx unpacks this package into a cache folder under ~/.npm. That cache can be
 * wiped, so the pip venv goes in ~/.callwalkietalkie instead — durable across npx runs.
 */
function ensureVenv(runtime) {
  const venv = join(HOME, "venv");
  const python = join(venv, "bin", "python");
  const reqs = join(runtime, "requirements.txt");
  const stamp = join(HOME, "requirements.txt");

  mkdirSync(HOME, { recursive: true });

  const needCreate = !existsSync(python);
  const needUpdate =
    !needCreate &&
    existsSync(reqs) &&
    (!existsSync(stamp) || readFileSync(reqs, "utf8") !== readFileSync(stamp, "utf8"));

  if (!needCreate && !needUpdate) return python;

  const host = systemPython();
  if (!host) {
    fail("Python 3 is required. Install it from https://www.python.org/downloads/ and retry.");
  }

  if (needCreate) {
    process.stdout.write("  Installing local runtime (one-time)… ");
    run(host, ["-m", "venv", venv]);
    run(python, ["-m", "pip", "install", "--upgrade", "pip"]);
  } else {
    process.stdout.write("  Updating local runtime… ");
  }

  run(python, ["-m", "pip", "install", "-r", reqs]);
  writeFileSync(stamp, readFileSync(reqs, "utf8"));
  console.log("done.");
  return python;
}

function killExisting(port) {
  try {
    spawnSync("pkill", ["-f", "winproxy.py"]);
  } catch {
    /* ignore */
  }
  try {
    const r = spawnSync("lsof", ["-ti", `tcp:${port}`], { encoding: "utf8" });
    for (const pid of (r.stdout || "").trim().split("\n").filter(Boolean)) {
      try {
        process.kill(Number(pid), "SIGTERM");
      } catch {
        /* ignore */
      }
    }
  } catch {
    /* ignore */
  }
}

async function ensureMachineKey(site) {
  const keyPath = join(HOME, "key");
  mkdirSync(HOME, { recursive: true });
  if (existsSync(keyPath)) {
    const existing = readFileSync(keyPath, "utf8").trim();
    if (/^cwt_[a-f0-9]{48,128}$/i.test(existing)) return existing;
  }

  process.stdout.write("  Minting machine key… ");
  let res;
  try {
    res = await fetch(`${site}/v1/keys`, {
      method: "POST",
      signal: AbortSignal.timeout(15000),
    });
  } catch (e) {
    fail(`Could not reach ${site} to mint a key (${e.message || e}).`);
  }
  if (!res.ok) fail(`Key mint failed (${res.status}) from ${site}.`);
  const body = await res.json();
  const key = String(body.key || "").trim();
  if (!/^cwt_[a-f0-9]{48,128}$/i.test(key)) {
    fail("Relay returned an invalid machine key.");
  }
  writeFileSync(keyPath, `${key}\n`, { mode: 0o600 });
  console.log(`saved (${body.fingerprint || "ok"}).`);
  return key;
}

function start(python, runtime, port, key) {
  const log = join(tmpdir(), "callwalkietalkie.log");
  const out = openSync(log, "a");
  spawn(python, ["winproxy.py", "--no-open"], {
    cwd: runtime,
    detached: true,
    stdio: ["ignore", out, out],
    env: {
      ...process.env,
      LONGLEASH_PORT: String(port),
      LONGLEASH_TOKEN: TOKEN,
      LONGLEASH_KEY: key,
      CALLWALKIETALKIE_PORT: String(port),
      CALLWALKIETALKIE_TOKEN: TOKEN,
      CALLWALKIETALKIE_KEY: key,
    },
  }).unref();
  return log;
}

async function waitUntilUp(port, seconds = 45) {
  const deadline = Date.now() + seconds * 1000;
  while (Date.now() < deadline) {
    const state = await probe(port);
    if (state?.code && state.ready !== false) return state;
    await new Promise((r) => setTimeout(r, 350));
  }
  return null;
}

function openInBrowser(url) {
  const [cmd, args] =
    process.platform === "darwin"
      ? ["open", [url]]
      : process.platform === "win32"
        ? ["cmd", ["/c", "start", "", url]]
        : ["xdg-open", [url]];
  try {
    spawn(cmd, args, { stdio: "ignore", detached: true }).unref();
    return true;
  } catch {
    return false;
  }
}

async function main() {
  const flags = parseArgs(process.argv.slice(2));

  if (flags.version) {
    const { version } = JSON.parse(readFileSync(join(PACKAGE_ROOT, "package.json"), "utf8"));
    console.log(version);
    return;
  }
  if (flags.help) {
    console.log(USAGE);
    return;
  }

  if (process.platform !== "darwin") {
    fail("callwalkietalkie currently needs macOS + cmux.");
  }

  const port = Number(flags.port || PORT);
  const localSetup = `http://127.0.0.1:${port}/setup?t=${encodeURIComponent(TOKEN)}`;
  const site = (
    process.env.CALLWALKIETALKIE_SITE ||
    process.env.LONGLEASH_SITE ||
    "https://callwalkietalkie.com"
  ).replace(/\/$/, "");

  let state = flags.fresh ? null : await probe(port);

  if (flags.fresh && (await probe(port))) {
    process.stdout.write("\n  Restarting… ");
    killExisting(port);
    await new Promise((r) => setTimeout(r, 800));
    state = null;
    console.log("ok.");
  }

  if (!state) {
    const runtime = runtimeDir();
    if (!runtime) {
      fail(
        "This package is missing its server runtime. Reinstall with: npm i -g callwalkietalkie@latest",
      );
    }
    console.log("");
    const key = await ensureMachineKey(site);
    const python = ensureVenv(runtime);
    process.stdout.write("  Starting… ");
    const log = start(python, runtime, port, key);
    state = await waitUntilUp(port);
    if (!state) {
      let extra = "";
      try {
        extra = readFileSync(log, "utf8").trim().split("\n").slice(-12).join("\n");
        if (extra) extra = `\n\n${extra}`;
      } catch {
        /* ignore */
      }
      fail(`Nothing came up on port ${port}. See ${log}${extra}`);
    }
    console.log("ready.");
  } else {
    console.log("\n  Already running.");
  }

  console.log(`  Session ${state.code} on ${machineName()}.`);
  if (state.fingerprint) console.log(`  Key ${state.fingerprint}`);
  if (!lanAddress()) {
    console.log("  No wifi address found — your phone may not reach this Mac.");
  }

  const pairUrl =
    state.pair_url || (state.code ? `${site}/pair/${state.code}` : localSetup);
  if (!flags["no-open"] && openInBrowser(pairUrl)) {
    console.log("  Opening callwalkietalkie.com — scan the QR with your phone.\n");
  } else {
    console.log(`  Open this and scan the QR:\n  ${pairUrl}\n`);
  }
}

main();
