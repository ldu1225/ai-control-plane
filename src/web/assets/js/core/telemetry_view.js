/**
 * Shared text helpers for telemetry sources and their dashboards (Telemetry Extensions and widget boards).
 * Catalog text comes from the server in English and is translated here.
 */
import { t } from './i18n.js';

/** gRPC canonical codes, used to explain numeric error codes such as 13 (INTERNAL). */
export const GRPC_CODES = ['OK', 'CANCELLED', 'UNKNOWN', 'INVALID_ARGUMENT', 'DEADLINE_EXCEEDED', 'NOT_FOUND', 'ALREADY_EXISTS',
  'PERMISSION_DENIED', 'RESOURCE_EXHAUSTED', 'FAILED_PRECONDITION', 'ABORTED', 'OUT_OF_RANGE', 'UNIMPLEMENTED', 'INTERNAL',
  'UNAVAILABLE', 'DATA_LOSS', 'UNAUTHENTICATED'];

export function cardTitle(c) {
  return c && c.title ? t(c.title, c.titleParams || undefined) : '';
}

export function cardWhat(c) {
  return c && c.what ? t(c.what, c.whatParams || undefined) : '';
}

/** Shortens long identifiers for chart labels; the full value stays in the tooltip and the tables. */
export function shortValue(v) {
  if (v === null || v === undefined || v === '') return t('Unknown');
  const s = String(v);
  const slash = s.split('/');
  if (slash.length > 3) return slash.slice(-2).join('/');
  const dots = s.split('.');
  if (dots.length > 3 && /^[A-Za-z_][\w.]*$/.test(s)) return dots.slice(-2).join('.');
  return s;
}

/** A status value for people: numeric error codes get their gRPC name. */
export function statusText(v, labelKey) {
  if (v === null || v === undefined || v === '') return t('Unknown');
  const s = String(v);
  if (labelKey === 'Error code' && /^\d{1,2}$/.test(s) && GRPC_CODES[Number(s)]) return `${s} ${GRPC_CODES[Number(s)]}`;
  return shortValue(s);
}
