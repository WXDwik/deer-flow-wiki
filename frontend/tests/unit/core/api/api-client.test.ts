import { expect, test } from "vitest";

import { isRunNotFoundError } from "@/core/api/api-client";

test("identifies stale LangGraph run 404s", () => {
  const error = new Error(
    'HTTP 404: {"detail":"Run 7cdd7896-e1be-4e0e-b14e-a7630e2a0e8c not found"}',
  );
  Reflect.set(error, "status", 404);

  expect(isRunNotFoundError(error)).toBe(true);
});

test("does not treat unrelated 404s as stale run errors", () => {
  const error = new Error('HTTP 404: {"detail":"Thread not found"}');
  Reflect.set(error, "status", 404);

  expect(isRunNotFoundError(error)).toBe(false);
});
