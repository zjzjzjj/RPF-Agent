import type { ReactNode } from "react";

export interface DashboardMetric {
  label: string;
  value: ReactNode;
  hint: string;
  icon: ReactNode;
  tone?: "green" | "amber" | "blue";
}

export default function DashboardMetrics({
  items,
}: {
  items: DashboardMetric[];
}) {
  return (
    <div className="metrics-grid">
      {items.map((item) => (
        <div className="metric-card" key={item.label}>
          <div className="metric-top">
            <span>{item.label}</span>
            <span className={`metric-icon ${item.tone ?? "green"}`}>
              {item.icon}
            </span>
          </div>
          <div className="metric-value">{item.value}</div>
          <div className="metric-hint">{item.hint}</div>
        </div>
      ))}
    </div>
  );
}
