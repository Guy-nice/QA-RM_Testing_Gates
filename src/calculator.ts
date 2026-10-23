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
