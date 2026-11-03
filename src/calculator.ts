export function add(a: number, b: number): number {
  return a + b;
}

export function subtract(a: number, b: number): number {
  return a - b;
}

export function isPositive(n: number): boolean {
  if (n > 0) {
    return true;
  }
  return false;
}

export function divide(a: number, b: number): number {
  if (b === 0) {
    throw new Error("Division by zero");
  }
  return a / b;
}

export function multiply(a: number, b: number): number {
  return a * b;
}

export function isEven(n: number): boolean {
  return n % 2 === 0;
}

export function square(n: number): number {
  return n * n;
}

export function modulo(a: number, b: number): number {
  if (b === 0) {
    throw new Error("Modulo by zero");
  }
  return a % b;
}

export function cube(n: number): number {
  return n * n * n;
}

export function factorial(n: number): number {
  if (n < 0) {
    throw new Error("Factorial of negative number");
  }
  if (n === 0 || n === 1) {
    return 1;
  }
  return n * factorial(n - 1);
}

export function sign(n: number): number {
  if (n > 0) {
    return 1;
  }
  if (n < 0) {
    return -1;
  }
  return 0;
}

export function clamp(n: number, min: number, max: number): number {
  if (n < min) {
    return min;
  }
  if (n > max) {
    return max;
  }
  return n;
}

export function isLeapYear(year: number): boolean {
  if (year % 400 === 0) {
    return true;
  }
  if (year % 100 === 0) {
    return false;
  }
  return year % 4 === 0;
}

export function between(n: number, low: number, high: number): boolean {
  return n >= low && n <= high;
}

export function roundTo(n: number, digits: number): number {
  const factor = 10 ** digits;
  return Math.round(n * factor) / factor;
}

export function average(values: number[]): number {
  if (values.length === 0) {
    throw new Error("Average of an empty list");
  }
  return values.reduce((sum, v) => sum + v, 0) / values.length;
}
