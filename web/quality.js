// Network adaptation, kept free of the DOM so it can be unit-tested.
// The sharer samples each viewer's connection every 2 s and picks one of these levels.

export const LEVELS = [
  { name: 'full', scale: 1, maxBitrate: 8_000_000 },
  { name: 'three-quarter', scale: 4 / 3, maxBitrate: 4_000_000 },
  { name: 'half', scale: 2, maxBitrate: 2_000_000 },
  { name: 'saver', scale: 2, maxBitrate: 1_000_000 },
];

const BAD_TO_STEP_DOWN = 2;
const GOOD_TO_STEP_UP = 5;
const COOLDOWN_SAMPLES = 3;

export function newTuner() {
  return { level: 0, bad: 0, good: 0, cooldown: 0 };
}

// mode: 'auto' adapts, 'high' pins full quality, 'saver' pins the lowest level.
export function decide(tuner, sample, mode = 'auto') {
  const last = LEVELS.length - 1;
  if (mode === 'high') return { level: 0, bad: 0, good: 0, cooldown: 0 };
  if (mode === 'saver') return { level: last, bad: 0, good: 0, cooldown: 0 };
  if (tuner.cooldown > 0) return { ...tuner, bad: 0, good: 0, cooldown: tuner.cooldown - 1 };
  const cap = LEVELS[tuner.level].maxBitrate;
  // Little spare bandwidth only matters when the encoder says bandwidth holds it back:
  // a still screen simply has little to send.
  const squeezed = sample.limited === 'bandwidth' && sample.available > 0 && sample.available < 0.5 * cap;
  const isBad = sample.loss > 0.05 || sample.rtt > 0.3 || squeezed;
  const isGood = !isBad && sample.loss < 0.01 && sample.rtt < 0.15;
  const bad = isBad ? tuner.bad + 1 : 0;
  const good = isGood ? tuner.good + 1 : 0;
  if (bad >= BAD_TO_STEP_DOWN && tuner.level < last) {
    return { level: tuner.level + 1, bad: 0, good: 0, cooldown: COOLDOWN_SAMPLES };
  }
  if (good >= GOOD_TO_STEP_UP && tuner.level > 0) {
    return { level: tuner.level - 1, bad: 0, good: 0, cooldown: COOLDOWN_SAMPLES };
  }
  return { level: tuner.level, bad, good, cooldown: 0 };
}

function selectedPair(stats) {
  for (const report of stats.values()) {
    if (report.type === 'transport' && report.selectedCandidatePairId && stats.get) {
      const pair = stats.get(report.selectedCandidatePairId);
      if (pair) return pair;
    }
  }
  for (const report of stats.values()) {
    if (report.type === 'candidate-pair' && report.nominated && report.state === 'succeeded') return report;
  }
  return null;
}

// Sender-side sample from RTCPeerConnection.getStats(); `prev` is the previous sample (or null).
export function senderSample(prev, stats, now) {
  let out = null;
  let remote = null;
  for (const report of stats.values()) {
    if (report.type === 'outbound-rtp' && report.kind === 'video') out = report;
    else if (report.type === 'remote-inbound-rtp' && report.kind === 'video') remote = report;
  }
  const pair = selectedPair(stats);
  const bytes = out?.bytesSent ?? 0;
  const seconds = prev ? (now - prev.time) / 1000 : 0;
  const bitrate = prev && seconds > 0 ? Math.max(0, ((bytes - prev.bytes) * 8) / seconds) : 0;
  return {
    time: now,
    bytes,
    bitrate,
    fps: out?.framesPerSecond ?? 0,
    width: out?.frameWidth ?? 0,
    height: out?.frameHeight ?? 0,
    loss: remote?.fractionLost ?? 0,
    rtt: remote?.roundTripTime ?? pair?.currentRoundTripTime ?? 0,
    available: pair?.availableOutgoingBitrate ?? 0,
    limited: out?.qualityLimitationReason ?? 'none',
  };
}

// Viewer-side sample: packet loss since the previous sample and the decoded frame rate.
export function receiverSample(prev, stats) {
  let inbound = null;
  for (const report of stats.values()) {
    if (report.type === 'inbound-rtp' && report.kind === 'video') inbound = report;
  }
  const packetsLost = inbound?.packetsLost ?? 0;
  const packetsReceived = inbound?.packetsReceived ?? 0;
  let loss = 0;
  if (prev) {
    const lost = Math.max(0, packetsLost - prev.packetsLost);
    const received = Math.max(0, packetsReceived - prev.packetsReceived);
    if (lost + received > 0) loss = lost / (lost + received);
  }
  return { packetsLost, packetsReceived, loss, fps: inbound?.framesPerSecond ?? 0 };
}

// The viewer's "Weak signal" badge, from one isWeak() answer every 2 s: shown after 2 weak samples in a row,
// hidden after 3 clean ones, so it doesn't blink (and isn't read out again and again) when loss hovers around the
// limit. `run` counts the samples in a row that disagree with what is shown.
export function weakBadge(badge = { shown: false, run: 0 }, weak = false) {
  if (weak === badge.shown) return { shown: badge.shown, run: 0 };
  const run = badge.run + 1;
  return run >= (badge.shown ? 3 : 2) ? { shown: weak, run: 0 } : { shown: badge.shown, run };
}

// Low frame rate alone is not weak: screen capture sends few frames when nothing moves.
export function isWeak(sample) {
  return sample.loss > 0.05 || sample.rtt > 0.3 || sample.limited === 'bandwidth';
}

export function describe(sample) {
  if (!sample || !sample.width) return '';
  const rate =
    sample.bitrate >= 1_000_000
      ? `${(sample.bitrate / 1_000_000).toFixed(1)} Mbps`
      : `${Math.round(sample.bitrate / 1000)} kbps`;
  return `${sample.width}×${sample.height} · ${Math.round(sample.fps)} fps · ${rate}`;
}
