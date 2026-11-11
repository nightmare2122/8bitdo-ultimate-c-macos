import Charts
import SwiftUI

func formatMs(_ value: Double?) -> String {
    guard let value else { return "–" }
    return value < 10 ? String(format: "%.1f", value) : String(format: "%.0f", value)
}

struct LatencyView: View {
    @Environment(BridgeModel.self) private var model

    var body: some View {
        if model.identity == .dualShock4 {
            HStack(alignment: .top, spacing: 12) {
                Image(systemName: "gauge.with.dots.needle.bottom.50percent")
                    .font(.title2)
                    .foregroundStyle(.secondary)
                    .accessibilityHidden(true)
                VStack(alignment: .leading, spacing: 3) {
                    Text("Latency needs Xbox mode")
                        .font(.headline)
                    Text("The DualShock 4 mode can’t answer latency checks. Switch PadBridge Host to Xbox mode to measure the round trip.")
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            .padding(.vertical, 6)
        } else {
            XboxLatency()
        }
    }
}

private struct XboxLatency: View {
    @Environment(BridgeModel.self) private var model

    var body: some View {
        let stats = model.latency
        VStack(alignment: .leading, spacing: 14) {
            HStack(spacing: 0) {
                StatTile(title: "Last", value: formatMs(stats.last), unit: "ms", emphasized: true)
                Divider().frame(height: 36)
                StatTile(title: "Median", value: formatMs(stats.median), unit: "ms")
                Divider().frame(height: 36)
                StatTile(title: "95th percentile", value: formatMs(stats.p95), unit: "ms")
                Divider().frame(height: 36)
                StatTile(title: "Lost", value: stats.sent == 0 ? "–" : String(format: "%.0f", stats.lossPercent), unit: "%")
            }
            if stats.hostSilent && !stats.hasReplies {
                Label {
                    VStack(alignment: .leading, spacing: 2) {
                        Text("No reply from PadBridge Host")
                            .font(.headline)
                        Text("The laptop receives the checks but doesn’t answer them. Update PadBridge Host to a version that supports latency checks.")
                            .foregroundStyle(.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                } icon: {
                    Image(systemName: "exclamationmark.triangle.fill")
                        .symbolRenderingMode(.multicolor)
                }
            } else if !stats.hasReplies {
                HStack(spacing: 8) {
                    if !model.isMock { ProgressView().controlSize(.small) }
                    Text(model.isMock ? "Not measured in preview mode." : model.isSampling ? "Measuring…" : "Measuring starts when PadBridge is on screen.")
                        .foregroundStyle(.secondary)
                }
                .frame(maxWidth: .infinity, minHeight: 60)
            } else {
                VStack(alignment: .leading, spacing: 6) {
                    LatencyChart(samples: stats.samples, median: stats.median)
                        .frame(height: 120)
                    HStack(spacing: 14) {
                        LegendSwatch(dashed: false, text: "Round trip, last 10 seconds")
                        LegendSwatch(dashed: true, text: "Median")
                    }
                    .font(.caption)
                    .foregroundStyle(.secondary)
                }
            }
        }
        .padding(.vertical, 4)
    }
}

struct StatTile: View {
    let title: String
    let value: String
    let unit: String
    var emphasized = false

    var body: some View {
        VStack(spacing: 2) {
            HStack(alignment: .firstTextBaseline, spacing: 2) {
                Text(value)
                    .font(.system(.title2, design: .rounded).weight(emphasized ? .semibold : .medium))
                    .monospacedDigit()
                Text(unit)
                    .font(.callout)
                    .foregroundStyle(.secondary)
            }
            Text(title)
                .font(.caption)
                .foregroundStyle(.secondary)
                .lineLimit(1)
        }
        .frame(maxWidth: .infinity)
        .accessibilityElement(children: .combine)
    }
}

struct LatencyChart: View {
    let samples: [LatencySample]
    let median: Double?
    var compact = false

    /// Round the top of the scale up to a tidy value so the axis doesn't jump on every sample.
    private var yMax: Double {
        let peak = samples.map(\.ms).max() ?? 20
        let step: Double = peak > 100 ? 50 : 20
        return max(step, (peak / step).rounded(.up) * step)
    }

    var body: some View {
        let first = samples.first?.id ?? 0
        let end = max(first + LatencyStats.window - 1, samples.last?.id ?? 0)
        Chart {
            LinePlot(samples, x: .value("Sample", \.id), y: .value("Round trip", \.ms))
                .lineStyle(StrokeStyle(lineWidth: compact ? 1.25 : 1.5, lineJoin: .round))
                .foregroundStyle(Color.accentColor)
            if let median, !compact {
                RuleMark(y: .value("Median", median))
                    .lineStyle(StrokeStyle(lineWidth: 1, dash: [4, 3]))
                    .foregroundStyle(Color.secondary)
            }
        }
        .chartXScale(domain: first...end)
        .chartYScale(domain: 0...yMax)
        .chartXAxis(.hidden)
        .chartYAxis(compact ? .hidden : .automatic)
        .chartYAxis {
            if !compact {
                AxisMarks(position: .leading) { value in
                    AxisGridLine()
                    AxisValueLabel { if let v = value.as(Double.self) { Text("\(Int(v)) ms") } }
                }
            }
        }
        .accessibilityLabel("Latency over the last \(samples.count) samples")
        .accessibilityValue(median.map { "Median \(formatMs($0)) milliseconds" } ?? "")
    }
}

struct RumbleView: View {
    @Environment(BridgeModel.self) private var model

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 8) {
                Button { model.rumble(.strong) } label: { Label("Strong motor", systemImage: "waveform.path") }
                Button { model.rumble(.weak) } label: { Label("Weak motor", systemImage: "waveform") }
                Button { model.rumble(.both) } label: { Label("Both motors", systemImage: "waveform.badge.plus") }
                Spacer(minLength: 0)
            }
            .controlSize(.large)
            HStack(spacing: 6) {
                switch model.rumbleStatus {
                case .sent(let motor)?:
                    Image(systemName: "checkmark.circle.fill").foregroundStyle(.green)
                    Text(motor == .both ? "Sent to both motors." : "Sent to the \(motor.rawValue) motor.")
                case .failed(_, let code)?:
                    Image(systemName: "xmark.octagon.fill").foregroundStyle(.red)
                    Text(String(format: "Couldn’t send the rumble (error 0x%08X). Check that the laptop is still connected.", code))
                case nil:
                    Text("Each test buzzes the controller on the laptop for 0.4 seconds.")
                        .foregroundStyle(.secondary)
                }
            }
            .font(.callout)
            .animation(.default, value: model.rumbleStatus)
        }
        .padding(.vertical, 4)
    }
}

struct LegendSwatch: View {
    let dashed: Bool
    let text: String

    var body: some View {
        HStack(spacing: 5) {
            Path { p in p.move(to: CGPoint(x: 0, y: 4)); p.addLine(to: CGPoint(x: 14, y: 4)) }
                .stroke(dashed ? AnyShapeStyle(Color.secondary) : AnyShapeStyle(Color.accentColor),
                        style: StrokeStyle(lineWidth: dashed ? 1 : 2, dash: dashed ? [3, 2] : []))
                .frame(width: 14, height: 8)
            Text(text)
        }
        .accessibilityElement(children: .combine)
    }
}
