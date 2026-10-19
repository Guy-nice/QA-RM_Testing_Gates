import { test } from "node:test";
import assert from "node:assert/strict";
import { add, subtract, isPositive, divide, multiply, isEven } from "./calculator.ts";

test("add adds two numbers", () => {
  assert.equal(add(2, 3), 5);
});

test("subtract subtracts two numbers", () => {
  assert.equal(subtract(5, 3), 2);
});

test("isPositive returns true for positive numbers", () => {
  assert.equal(isPositive(4), true);
});

test("isPositive returns false for zero", () => {
  assert.equal(isPositive(0), false);
});

test("divide divides two numbers", () => {
  assert.equal(divide(10, 2), 5);
});

test("divide throws on division by zero", () => {
  assert.throws(() => divide(1, 0), /Division by zero/);
});

test("multiply multiplies two numbers", () => {
  assert.equal(multiply(3, 4), 12);
});

test("multiply returns zero when multiplying by zero", () => {
  assert.equal(multiply(5, 0), 0);
});

test("isEven returns true for even numbers", () => {
  assert.equal(isEven(4), true);
  assert.equal(isEven(0), true);
});

test("isEven returns false for odd numbers", () => {
  assert.equal(isEven(3), false);
  assert.equal(isEven(1), false);
});
