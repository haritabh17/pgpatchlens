import { execFile } from "node:child_process";

const FROM = "git+https://github.com/haritabh17/pg_patchlens";

export default function patchlensExtension(pi) {
  pi.registerCommand("pgpatchlens", {
    description: "Review a PostgreSQL commitfest patch in the pg_patchlens web UI",
    handler: async (args, ctx) => {
      const link = String(args || "").trim();
      ctx?.ui?.notify?.("pg_patchlens: starting the local server…", "info");
      const argv = ["--from", FROM, "pgpatchlens", "open", ...(link ? [link] : [])];
      execFile("uvx", argv, { timeout: 180000 }, (err, stdout, stderr) => {
        if (err) {
          const detail = String(stderr || err).slice(0, 300);
          ctx?.ui?.notify?.(`pg_patchlens failed (is uv installed?): ${detail}`, "error");
          return;
        }
        const url = String(stdout).trim().split("\n").pop();
        ctx?.ui?.notify?.(`pg_patchlens: ${url} (new entries stream analysis live, ~3 min)`, "info");
      });
    },
  });
}
