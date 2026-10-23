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

export function modulo(a: number, b: number): number {
  if (b === 0) {
    throw new Error("Modulo by zero");
  }
  return a % b;
}

export function power(base: number, exponent: number): number {
  return Math.pow(base, exponent);
}

export function absolute(n: number): number {
  if (n < 0) {
    return -n;
  }
  return n;
}

export function max(a: number, b: number): number {
  if (a > b) {
    return a;
  }
  return b;
}
