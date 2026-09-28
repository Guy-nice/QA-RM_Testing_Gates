import { test } from "node:test";
import assert from "node:assert/strict";
import { add, subtract } from "./calculator.ts";

test("add adds two numbers", () => {
  assert.equal(add(2, 3), 5);
});

test("subtract subtracts two numbers", () => {
  assert.equal(subtract(5, 3), 2);
});
