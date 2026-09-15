// @ts-nocheck
// Test-only ESM loader: compile the actual components, without rewriting their logic.
import { readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { compile } from 'svelte/compiler';
import ts from 'typescript';

export async function resolve(specifier, context, nextResolve) {
  if (specifier === '$app/state') {
    return { url: 'data:text/javascript,export const page = { url: new URL("http://localhost/downloads") };', shortCircuit: true };
  }
  if (specifier === '$app/navigation') {
    return { url: 'data:text/javascript,export async function goto() {}', shortCircuit: true };
  }
  if (specifier === 'svelte' || specifier.startsWith('svelte/')) {
    return nextResolve(specifier, { ...context, conditions: [...context.conditions, 'browser'] });
  }
  if (specifier.startsWith('$lib/')) {
    specifier = new URL(`../src/lib/${specifier.slice(5)}`, import.meta.url).href;
  }
  try {
    return await nextResolve(specifier, context);
  } catch (error) {
    if (error.code !== 'ERR_MODULE_NOT_FOUND') throw error;
    return nextResolve(`${specifier}.ts`, context);
  }
}

export async function load(url, context, nextLoad) {
  if (url.endsWith('.css')) return { format: 'module', shortCircuit: true, source: '' };
  if (url.endsWith('.svelte')) {
    const source = await readFile(new URL(url), 'utf8');
    return {
      format: 'module', shortCircuit: true,
      source: compile(source, { filename: fileURLToPath(url), generate: 'client', css: 'injected' }).js.code,
    };
  }
  if (url.endsWith('.ts')) {
    return {
      format: 'module', shortCircuit: true,
      source: ts.transpileModule(await readFile(new URL(url), 'utf8'), {
        compilerOptions: { target: ts.ScriptTarget.ESNext, module: ts.ModuleKind.ESNext },
      }).outputText,
    };
  }
  return nextLoad(url, context);
}
