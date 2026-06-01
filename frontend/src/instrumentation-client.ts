const PATCHED_MEASURE_SYMBOL = Symbol.for("deer-flow.performance.measure");

type Measure = (
  name: string,
  startOrMeasureOptions?: string | number | PerformanceMeasureOptions,
  endMark?: string,
) => PerformanceMeasure;

function normalizeTimestamp<T>(value: T): T | number {
  return typeof value === "number" && value < 0 ? 0 : value;
}

function normalizeMeasureOptions(
  options: PerformanceMeasureOptions,
): PerformanceMeasureOptions {
  return {
    ...options,
    start: normalizeTimestamp(options.start),
    end: normalizeTimestamp(options.end),
    duration: normalizeTimestamp(options.duration),
  };
}

function patchPerformanceMeasure() {
  if (typeof performance === "undefined") {
    return;
  }

  const measure = Reflect.get(performance, "measure") as Measure | undefined;
  if (
    typeof measure !== "function" ||
    Reflect.get(measure, PATCHED_MEASURE_SYMBOL)
  ) {
    return;
  }

  const originalMeasure = measure.bind(performance);
  const patchedMeasure: Measure = (name, startOrMeasureOptions?, endMark?) => {
    if (
      typeof startOrMeasureOptions === "object" &&
      startOrMeasureOptions !== null
    ) {
      return originalMeasure(
        name,
        normalizeMeasureOptions(startOrMeasureOptions),
      );
    }

    return originalMeasure(
      name,
      normalizeTimestamp(startOrMeasureOptions),
      endMark,
    );
  };

  Reflect.set(patchedMeasure, PATCHED_MEASURE_SYMBOL, true);
  performance.measure = patchedMeasure;
}

if (process.env.NODE_ENV === "development") {
  patchPerformanceMeasure();
}
