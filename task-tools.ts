// Copyright 2026 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

/**
 * Routes pi's read, write, edit, and bash tools to the task container through
 * the taskbridge Relay (TASK_RELAY_URL). The tools resolve relative paths and
 * run commands in the task's working directory (TASK_WORKDIR).
 */
import {
  type ExtensionAPI,
  createBashToolDefinition,
  createEditToolDefinition,
  createReadToolDefinition,
  createWriteToolDefinition,
} from '@earendil-works/pi-coding-agent';
import {Type} from 'typebox';
import {spawn} from 'node:child_process';
import {readFile} from 'node:fs/promises';
import http from 'node:http';
import {tmpdir} from 'node:os';
import path from 'node:path';

const RELAY = process.env.TASK_RELAY_URL ?? "";
const WORKDIR = process.env.TASK_WORKDIR ?? "/";
const MAX_EXEC_SEC = 600;
const IMAGES: Record<string, string> = {
	".png": "image/png",
	".jpg": "image/jpeg",
	".jpeg": "image/jpeg",
	".gif": "image/gif",
	".webp": "image/webp",
};

interface RelayResponse {
	ok?: boolean;
	error?: string;
	truncated?: boolean;
	size?: number;
	content_b64?: string;
	output?: string;
	timed_out?: boolean;
	exit_code: number;
}

function relay(route: string, body: object): Promise<RelayResponse> {
	const url = new URL(`${RELAY}/${route}`);
	const data = Buffer.from(JSON.stringify(body));
	return new Promise((resolve, reject) => {
		const req = http.request(
			{
				hostname: url.hostname,
				port: url.port,
				path: url.pathname,
				method: "POST",
				headers: { "Content-Type": "application/json", "Content-Length": data.length },
			},
			(res) => {
				const chunks: Buffer[] = [];
				res.on("data", (chunk) => chunks.push(chunk));
				res.on("end", () => {
					try {
						const out = JSON.parse(Buffer.concat(chunks).toString() || "{}") as RelayResponse;
						if (res.statusCode !== 200) reject(new Error(out.error ?? `relay answered ${res.statusCode}`));
						else if (out.ok === false) reject(new Error(out.error));
						else resolve(out);
					} catch (err) {
						reject(err);
					}
				});
			},
		);
		req.on("error", reject);
		req.end(data);
	});
}

function quote(s: string): string {
	return `'${s.replace(/'/g, `'\\''`)}'`;
}

/**
 * Wraps `tool` so relative paths (e.g., "../data.csv") resolve and commands
 * start in `workdir`.
 *
 * `pi` uses the cwd of the context passed to a tool (which defaults to pi's own
 * scratch directory), so the tool receives a derived context whose `cwd` is
 * `workdir`. `Object.create` preserves the context's other properties, which pi
 * reads lazily.
 */
// tslint:disable-next-line:no-any
export function inTask(tool: any, workdir: string): any {
	return {
		...tool,
		// tslint:disable-next-line:no-any
		execute: (id: string, params: unknown, signal: unknown, onUpdate: unknown, ctx: any) =>
			tool.execute(id, params, signal, onUpdate, ctx && Object.create(ctx, { cwd: { value: workdir } })),
	};
}

interface CalculatorResponse {
	ok: boolean;
	result?: Record<string, unknown>;
	error?: string;
}

function calculate(
	tool: string,
	parameters: Record<string, number>,
	signal: AbortSignal | undefined,
): Promise<CalculatorResponse> {
	const python = process.env.AEGIS_PYTHON ?? 'python3';
	const script = process.env.AEGIS_CALCULATOR_PATH;
	if (!script) return Promise.reject(new Error('calculator path is unavailable'));
	return new Promise((resolve, reject) => {
		const child = spawn(python, [script], {
			stdio: ['pipe', 'pipe', 'ignore'],
			env: {
				PATH: process.env.PATH ?? '',
				PYTHONIOENCODING: 'utf-8',
			},
		});
		const chunks: Buffer[] = [];
		let outputBytes = 0;
		let settled = false;
		const finishError = (error: Error) => {
			if (settled) return;
			settled = true;
			clearTimeout(timer);
			signal?.removeEventListener('abort', abort);
			child.kill('SIGKILL');
			reject(error);
		};
		const abort = () => finishError(new Error('calculation cancelled'));
		const timer = setTimeout(() => finishError(new Error('calculation timed out')), 10000);
		child.stdout.on('data', (chunk: Buffer) => {
			outputBytes += chunk.length;
			if (outputBytes > 65536) {
				finishError(new Error('calculator output exceeded limit'));
				return;
			}
			chunks.push(chunk);
		});
		child.once('error', () => finishError(new Error('calculator process failed')));
		child.once('close', (code) => {
			if (settled) return;
			settled = true;
			clearTimeout(timer);
			signal?.removeEventListener('abort', abort);
			if (code !== 0) {
				reject(new Error('calculator process failed'));
				return;
			}
			try {
				resolve(JSON.parse(Buffer.concat(chunks).toString('utf8')) as CalculatorResponse);
			} catch {
				reject(new Error('calculator returned invalid output'));
			}
		});
		signal?.addEventListener('abort', abort, {once: true});
		if (signal?.aborted) {
			abort();
			return;
		}
		child.stdin.end(JSON.stringify({tool, parameters}));
	});
}

function registerEngineeringTools(pi: ExtensionAPI) {
	pi.registerTool({
		name: 'pipe_hydraulics',
		label: 'Pipe hydraulics',
		description:
			'Calculate steady full-pipe Darcy-Weisbach head loss and pressure drop. SI units only. Transitional flow is flagged. This is a calculation, not code approval.',
		parameters: Type.Object({
			flow_m3_s: Type.Number({description: 'Volumetric flow rate in m^3/s'}),
			diameter_m: Type.Number({description: 'Internal pipe diameter in m'}),
			length_m: Type.Number({description: 'Straight pipe length in m'}),
			roughness_m: Type.Number({description: 'Absolute internal roughness in m'}),
			kinematic_viscosity_m2_s: Type.Optional(Type.Number({description: 'Kinematic viscosity in m^2/s; defaults to water at 20 C'})),
			density_kg_m3: Type.Optional(Type.Number({description: 'Fluid density in kg/m^3; defaults to water at 20 C'})),
			minor_loss_k: Type.Optional(Type.Number({description: 'Sum of fitting/local-loss K values; defaults to 0'})),
		}),
		async execute(_id, params, signal) {
			const response = await calculate('pipe_hydraulics', params, signal);
			return {
				content: [{type: 'text', text: JSON.stringify(response.result ?? response)}],
				details: response.result ?? {},
				isError: !response.ok,
			};
		},
	});

	pi.registerTool({
		name: 'beam_elastic_screen',
		label: 'Elastic beam screen',
		description:
			'Screen a prismatic simply supported beam under uniform load for elastic bending stress and deflection. SI units only. Does not check a structural design code.',
		parameters: Type.Object({
			span_m: Type.Number({description: 'Beam span in m'}),
			uniform_load_n_m: Type.Number({description: 'Uniform line load in N/m'}),
			elastic_modulus_pa: Type.Number({description: 'Elastic modulus in Pa'}),
			inertia_m4: Type.Number({description: 'Second moment of area in m^4'}),
			section_modulus_m3: Type.Number({description: 'Elastic section modulus in m^3'}),
			allowable_bending_pa: Type.Number({description: 'Project allowable bending stress in Pa'}),
			deflection_ratio: Type.Optional(Type.Number({description: 'Deflection limit denominator for L/n; defaults to 360'})),
		}),
		async execute(_id, params, signal) {
			const response = await calculate('beam_elastic_screen', params, signal);
			return {
				content: [{type: 'text', text: JSON.stringify(response.result ?? response)}],
				details: response.result ?? {},
				isError: !response.ok,
			};
		},
	});
}

/**
 * Returns a bash call's time limit in seconds: pi's `timeout` when it is a
 * positive number (or numeric string), capped at `max`. Zero, a negative
 * number, or an omitted value returns `max`, since 0 represents "no limit" in
 * pi.
 */
export function execLimit(timeout: unknown, max: number): number {
	const seconds = typeof timeout === "string" && timeout.trim() !== "" ? Number(timeout) : timeout;
	const usable = typeof seconds === "number" && Number.isFinite(seconds) && seconds > 0;
	return usable ? Math.max(1, Math.ceil(Math.min(seconds, max))) : max;
}

/**
 * Formats `exec` output as shown by a bash call, appending a truncation notice
 * at the end when the evaluator cut the middle of the output.
 */
export function execOutput(output: string, truncated: boolean): string {
	const note = "\n[the output was over 100 KB, so its middle was cut; write it to a file and read that in parts]\n";
	return truncated ? output + note : output;
}

/**
 * Returns true if `p` is pi's own temporary log file holding a long command's
 * output (`<tmp>/pi-bash-<16 hex>.log`), which stays local to the harness.
 */
export function isLocalTemp(p: string, tmp: string): boolean {
	return path.dirname(p) === tmp && /^pi-[a-z]+-[0-9a-f]{16}\.log$/.test(path.basename(p));
}

/**
 * Registers taskbridge-backed read, write, edit, and bash tools on the pi
 * coding agent extension API.
 */
// tslint:disable-next-line:no-default-export
export default function registerTaskTools(pi: ExtensionAPI) {
	registerEngineeringTools(pi);
	// pi saves a long command's output to a local temp file and points the model at it; reads of that file stay local.
	const isLocal = (p: string) => isLocalTemp(p, tmpdir());

	const readOp = async (p: string) => {
		if (isLocal(p)) return readFile(p);
		const r = await relay("read_file", { path: p });
		if (r.truncated) throw new Error(`${p} is ${r.size} bytes, over the 10 MB a read returns; use bash on it`);
		return Buffer.from(r.content_b64 ?? "", "base64");
	};
	const writeOp = async (p: string, content: string) => {
		await relay("write_file", { path: p, content_b64: Buffer.from(content).toString("base64") });
	};
	// No separate access check: readOp and writeOp report a missing or unwritable file, one round trip fewer.
	const access = async () => {};
	// tslint:disable-next-line:no-any
	const register = (tool: any) => pi.registerTool(inTask(tool, WORKDIR));

	register(
		createReadToolDefinition(WORKDIR, {
			operations: {
				readFile: readOp,
				access,
				detectImageMimeType: async (p) => IMAGES[path.extname(p).toLowerCase()] ?? null,
			},
		}),
	);
	// The relay's write_file creates missing parent directories.
	register(createWriteToolDefinition(WORKDIR, { operations: { writeFile: writeOp, mkdir: async () => {} } }));
	register(createEditToolDefinition(WORKDIR, { operations: { readFile: readOp, writeFile: writeOp, access } }));
	register(
		createBashToolDefinition(WORKDIR, {
			operations: {
				exec: async (command, cwd, { onData, signal, timeout }) => {
					if (signal?.aborted) throw new Error("aborted");
					const limit = execLimit(timeout, MAX_EXEC_SEC);
					const run = relay("exec", { command: `cd ${quote(cwd)} || exit 1\n${command}`, timeout_sec: limit });
					// The relay cannot cancel an exec: an abort stops the wait, and the command runs on to its time limit.
					const abort = new Promise<never>((_, reject) =>
						signal?.addEventListener("abort", () => reject(new Error("aborted")), { once: true }),
					);
					const r = await Promise.race([run, abort]);
					const output = execOutput(r.output ?? "", Boolean(r.truncated));
					if (output) onData(Buffer.from(output));
					if (r.timed_out) throw new Error(`timeout:${limit}`);
					return { exitCode: r.exit_code };
				},
			},
		}),
	);

	pi.on("before_agent_start", (event) => {
		event.systemPromptOptions.cwd = WORKDIR;
	});
}
