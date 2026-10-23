import { test } from "node:test";
import assert from "node:assert/strict";
import { add, subtract, isPositive, divide, multiply, isEven, square, modulo, cube, factorial, sign } from "./calculator.ts";

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

test("square returns the square of a number", () => {
  assert.equal(square(5), 25);
  assert.equal(square(2), 4);
});

test("modulo returns the remainder of division", () => {
  assert.equal(modulo(10, 3), 1);
  assert.equal(modulo(8, 2), 0);
});

test("modulo throws on modulo by zero", () => {
  assert.throws(() => modulo(5, 0), /Modulo by zero/);
});

test("cube returns the cube of a number", () => {
  assert.equal(cube(2), 8);
});

test("factorial computes factorial correctly", () => {
  assert.equal(factorial(0), 1);
  assert.equal(factorial(1), 1);
  assert.equal(factorial(2), 2);
  assert.equal(factorial(3), 6);
  assert.equal(factorial(4), 24);
  assert.equal(factorial(5), 120);
});

test("factorial throws on negative numbers", () => {
  assert.throws(() => factorial(-1), /Factorial of negative number/);
});

// Weak on purpose: only the positive branch is checked.
test("sign returns 1 for a positive number", () => {
  assert.equal(sign(5), 1);
});
