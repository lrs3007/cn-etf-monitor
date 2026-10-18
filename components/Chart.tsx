"use client";
import { useEffect, useRef } from "react";
import * as echarts from "echarts";

export default function Chart({ option, height = 260, label }: { option: echarts.EChartsOption; height?: number; label: string }) {
  const element = useRef<HTMLDivElement>(null);
  const instance = useRef<echarts.ECharts | null>(null);
  useEffect(() => {
    if (!element.current) return;
    const chart = echarts.init(element.current, undefined, { renderer: "canvas" });
    instance.current = chart;
    const observer = new ResizeObserver(() => chart.resize());
    observer.observe(element.current);
    return () => { observer.disconnect(); chart.dispose(); instance.current = null; };
  }, []);
  useEffect(() => { instance.current?.setOption(option, { notMerge: true }); }, [option]);
  return <div ref={element} style={{ height, width: "100%" }} role="img" aria-label={label} />;
}
