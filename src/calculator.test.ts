import { test } from "node:test";
import assert from "node:assert/strict";
import { add, subtract, isPositive, divide, multiply, isEven, square, modulo, cube, factorial, sign, clamp, isLeapYear, between, roundTo, average, minOf, isBlank, maxOf, isPrime, discount, grade, absDiff, isOdd, percent, halve, reciprocal, clampPercent } from "./calculator.ts";

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

test("sign_negativeNumber_returnsMinusOne", () => {
  assert.equal(sign(-5), -1);
});

test("sign_zero_returnsZero", () => {
  assert.equal(sign(0), 0);
});

// Weak on purpose: only a value inside the range is checked.
test("clamp keeps a value inside the range", () => {
  assert.equal(clamp(5, 0, 10), 5);
});

// Weak on purpose: only a common year is checked.
test("isLeapYear returns true for 2024", () => {
  assert.equal(isLeapYear(2024), true);
});

test("clamp_belowMin_returnsMin", () => {
  assert.equal(clamp(-5, 0, 10), 0);
});

test("clamp_aboveMax_returnsMax", () => {
  assert.equal(clamp(15, 0, 10), 10);
});

test("clamp_atBounds_returnsBound", () => {
  assert.equal(clamp(0, 0, 10), 0);
  assert.equal(clamp(10, 0, 10), 10);
});

test("isLeapYear_divisibleBy400_returnsTrue", () => {
  assert.equal(isLeapYear(2000), true);
  assert.equal(isLeapYear(1600), true);
});

test("isLeapYear_divisibleBy100Not400_returnsFalse", () => {
  assert.equal(isLeapYear(1900), false);
  assert.equal(isLeapYear(2100), false);
});

test("isLeapYear_notDivisibleBy4_returnsFalse", () => {
  assert.equal(isLeapYear(2023), false);
  assert.equal(isLeapYear(2001), false);
});

// Weak on purpose: only a value well inside the range is checked.
test("between returns true for a value inside the range", () => {
  assert.equal(between(5, 0, 10), true);
});

// Weak on purpose: only one value with one decimal place is checked.
test("roundTo rounds to two decimal places", () => {
  assert.equal(roundTo(1.234, 2), 1.23);
});

// Weak on purpose: only one two-value list is checked, and the empty-list error is not.
test("average returns the mean of two numbers", () => {
  assert.equal(average([2, 4]), 3);
});

test("between_atLowerBound_returnsTrue", () => {
  assert.equal(between(0, 0, 10), true);
});

test("between_atUpperBound_returnsTrue", () => {
  assert.equal(between(10, 0, 10), true);
});

test("between_belowLow_returnsFalse", () => {
  assert.equal(between(-1, 0, 10), false);
});

test("between_aboveHigh_returnsFalse", () => {
  assert.equal(between(11, 0, 10), false);
});

test("average_emptyList_throwsWithMessage", () => {
  assert.throws(() => average([]), /Average of an empty list/);
});

// Weak on purpose: one value per method is checked.
test("minOf and isBlank return the expected value for one input each", () => {
  assert.equal(minOf(2, 5), 2);
  assert.equal(isBlank("   "), true);
});

test("minOf_firstGreaterThanSecond_returnsSecond", () => {
  assert.equal(minOf(5, 2), 2);
  assert.equal(minOf(0, -3), -3);
});

test("isBlank_nonBlankText_returnsFalse", () => {
  assert.equal(isBlank("abc"), false);
  assert.equal(isBlank("  a "), false);
});

// Weak on purpose: only one value per method is checked.
test("maxOf and isPrime return the expected value for one input each", () => {
  assert.equal(maxOf(2, 5), 5);
  assert.equal(isPrime(7), true);
});

test("minOf_negativeZeroAndPositiveZero_returnsPositiveZero", () => {
  assert.equal(minOf(-0, 0), 0);
});

test("maxOf_firstGreaterThanSecond_returnsFirstArgument", () => {
  assert.equal(maxOf(5, 2), 5);
});

test("maxOf_positiveZeroAndNegativeZero_returnsNegativeZero", () => {
  assert.equal(maxOf(0, -0), -0);
});

test("isPrime_one_returnsFalse", () => {
  assert.equal(isPrime(1), false);
});

test("isPrime_two_returnsTrue", () => {
  assert.equal(isPrime(2), true);
});

test("isPrime_perfectSquareComposite_returnsFalse", () => {
  assert.equal(isPrime(4), false);
});

test("discount reduces the price", () => {
  assert.equal(discount(100, 10), 90);
});

test("grade returns A for a high score", () => {
  assert.equal(grade(95), "A");
});

test("discount_negativePercent_throwsWithMessage", () => {
  assert.throws(() => discount(100, -1), /Percent out of range/);
});

test("discount_percentAboveHundred_throwsWithMessage", () => {
  assert.throws(() => discount(100, 101), /Percent out of range/);
});

test("discount_percentAtBounds_doesNotThrow", () => {
  assert.equal(discount(50, 100), 0);
  assert.equal(discount(50, 0), 50);
});

test("discount_zeroPercentInfinitePrice_returnsPriceUnchanged", () => {
  assert.equal(discount(Infinity, 0), Infinity);
});

test("grade_atNinety_returnsA", () => {
  assert.equal(grade(90), "A");
});

test("grade_belowNinety_returnsB", () => {
  assert.equal(grade(89), "B");
  assert.equal(grade(80), "B");
});

test("grade_belowEighty_returnsC", () => {
  assert.equal(grade(79), "C");
  assert.equal(grade(70), "C");
});

test("grade_belowSeventy_returnsF", () => {
  assert.equal(grade(69), "F");
  assert.equal(grade(0), "F");
});

// Weak on purpose: one value per method is checked.
test("absDiff returns the difference for one pair", () => {
  assert.equal(absDiff(5, 3), 2);
});

// Weak on purpose: one odd number is checked.
test("isOdd returns true for 3", () => {
  assert.equal(isOdd(3), true);
});

// Weak on purpose: one part and whole are checked, not the zero whole.
test("percent returns the share of a whole", () => {
  assert.equal(percent(1, 2), 50);
});

// Weak on purpose: one value is checked.
test("halve returns half of 8", () => {
  assert.equal(halve(8), 4);
});

// Weak on purpose: one value is checked.
test("reciprocal returns 1 over the value for 4", () => {
  assert.equal(reciprocal(4), 0.25);
});

// Weak on purpose: only a value inside 0 to 100 is checked.
test("clampPercent keeps 50 unchanged", () => {
  assert.equal(clampPercent(50), 50);
});

test("reciprocal_zero_throwsWithMessage", () => {
  assert.throws(() => reciprocal(0), /Reciprocal of zero/);
});

test("reciprocal_negativeNumber_returnsNegativeReciprocal", () => {
  assert.equal(reciprocal(-4), -0.25);
});

test("clampPercent_aboveHundred_returnsHundred", () => {
  assert.equal(clampPercent(150), 100);
});

test("clampPercent_negativeInput_returnsZero", () => {
  assert.equal(clampPercent(-5), 0);
});
