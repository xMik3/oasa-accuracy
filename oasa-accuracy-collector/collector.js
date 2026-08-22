import mysql from "mysql2/promise";
import "dotenv/config";
import { readFileSync } from "fs";
import { fileURLToPath } from "url";
import path from "path";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROUTE_STOPS = JSON.parse(
  readFileSync(path.join(__dirname, "routes.json"), "utf8")
).routes;

const API = "https://telematics.oasa.gr/api/";
const ROUTES = Object.keys(ROUTE_STOPS);
const STOPS = [...new Set(Object.values(ROUTE_STOPS).flat())];
const INTERVAL_MS = Number(process.env.POLL_INTERVAL_SECONDS ?? 30) * 1000;

if (!STOPS.length) {
  console.error("No stops found in routes.json");
  process.exit(1);
}

if (!ROUTES.length) {
  console.error("No routes found in routes.json");
  process.exit(1);
}

const pool = mysql.createPool({
  host: process.env.DB_HOST ?? "database",
  port: Number(process.env.DB_PORT ?? 3306),
  user: process.env.DB_USER,
  password: process.env.DB_PASSWORD,
  database: process.env.DB_NAME,
  connectionLimit: 5,
  timezone: "Z",
});

//poll one stop
async function pollStop(stopCode) {
  let at = new Date();
  
  try {
    const resp = await fetch(
      `${API}?act=getStopArrivals&p1=${encodeURIComponent(stopCode)}`,
      { method: "POST", signal: AbortSignal.timeout(5_000) }
    );


    if (!resp.ok){
	    await resp.body?.cancel();
	    return { at, stopCode, status: "error" };
    }

    const text = await resp.text();

    let data;
    try { data = JSON.parse(text); }
    catch { 
	    return { at, stopCode, status: "error" }; 
    }
    if (!Array.isArray(data)){
	    return { at, stopCode, status: "empty" };
    }


    const rows = data.filter((b) => ROUTES.includes(String(b.route_code)));
    if (!rows.length){
	    return { at, stopCode, status: "empty" };
    }

    return { at, stopCode, status: "ok", rows };
  } catch(err){
    return { at, stopCode, status: "error" };
  }
}

async function runCycle() {

  const results = [];
  for(const stopCode of STOPS) results.push(await pollStop(stopCode));

  const obs = [];
  const log = [];

  for (const r of results) {
    log.push([r.at, r.stopCode, r.status]);
    for (const b of r.rows ?? []) {
      const eta = Number(b.btime2);
      if (!Number.isFinite(eta)) continue;
      obs.push([r.at, r.stopCode, String(b.route_code), String(b.veh_code), eta]);
    }
  }

  if (obs.length) {
    await pool.query(
      "INSERT IGNORE INTO eta_observations (fetched_at_utc, stop_code, route_code, veh_code, eta_minutes) VALUES ?",
      [obs]
    );
  }
  await pool.query(
    "INSERT INTO poll_log (fetched_at_utc, stop_code, status) VALUES ?",
    [log]
  );

  return {
    rows: obs.length,
    failed: results.filter((r) => r.status === "error").length,
  };
}

let running = true;
for (const sig of ["SIGINT", "SIGTERM"]) process.on(sig, () => (running = false));

console.log(`Polling ${STOPS.length} stops every ${INTERVAL_MS / 1000}s. Ctrl-C to stop.`);

let n = 0;
while (running) {
  const started = Date.now();
  n++;

  try {
    const { rows, failed } = await runCycle();
    if (n % 20 === 0 || failed) console.log(`cycle ${n}: ${rows} rows, ${failed} failed`);
  } catch (err) {
    // A DB blip must not end months of collection.
    console.error(`cycle ${n} failed:`, err.message);
  }

  // Subtract elapsed time so cycles keep a fixed cadence instead of drifting.
  await new Promise((r) => setTimeout(r, Math.max(0, INTERVAL_MS - (Date.now() - started))));
}

await pool.end();
console.log(`Stopped cleanly after ${n} cycles.`);
