import { Link } from '@tanstack/react-router';
import { useQuery } from '@tanstack/react-query';
import { useMemo } from 'react';
import { getDataSource } from '../lib/data-source';
import { ErrorState, LoadingState } from '../components/PageList';

/**
 * SVG canvas dimensions. A viewBox keeps the graph responsive
 * without re-computing positions on resize.
 */
const WIDTH = 800;
const HEIGHT = 600;
const ITERATIONS = 80;
/** Minimum node radius; grows with degree so hubs feel heavier. */
const BASE_RADIUS = 10;
const RADIUS_PER_LINK = 2.2;
/** Cap the radius so a hub node doesn't dwarf the canvas. */
const MAX_RADIUS = 26;

type Position = { x: number; y: number };

/** Palette for edge `rel` types. Falls back to `--color-muted`. */
const REL_COLORS: Record<string, string> = {
  references: 'var(--color-muted)',
  depends_on: 'var(--color-peach)',
  part_of: 'var(--color-sage)',
  supersedes: 'var(--color-rose)',
  contradicts: 'var(--color-lavender)',
};

function colorForRel(rel: string): string {
  return REL_COLORS[rel] ?? 'var(--color-muted)';
}

/**
 * Deterministic force-style layout: place nodes on a circle, then
 * run a tiny simulation (repulsion + spring) to separate connected
 * clusters. ~80 iterations is plenty for the small graphs a homelab
 * wiki produces and avoids pulling in d3 as a dependency.
 *
 * Pure: given the same node + edge input the output positions are
 * identical, so SSR snapshots and tests stay stable.
 */
function layout(
  nodeIds: number[],
  edges: { source: number; target: number }[],
): Map<number, Position> {
  const positions = new Map<number, Position>();
  if (nodeIds.length === 0) return positions;
  const cx = WIDTH / 2;
  const cy = HEIGHT / 2;
  const radius = Math.min(WIDTH, HEIGHT) / 2 - MAX_RADIUS - 20;
  nodeIds.forEach((id, index) => {
    const angle = (2 * Math.PI * index) / nodeIds.length;
    positions.set(id, {
      x: cx + radius * Math.cos(angle),
      y: cy + radius * Math.sin(angle),
    });
  });
  if (nodeIds.length < 2) return positions;

  const repulsionStrength = 1800;
  const springLength = 90;
  const springStrength = 0.04;
  const centerStrength = 0.012;
  const damping = 0.85;

  const velocities = new Map<number, { vx: number; vy: number }>();
  for (const id of nodeIds) velocities.set(id, { vx: 0, vy: 0 });

  for (let step = 0; step < ITERATIONS; step++) {
    // Repulsion between every pair.
    for (let i = 0; i < nodeIds.length; i++) {
      const a = nodeIds[i];
      const pa = positions.get(a)!;
      const va = velocities.get(a)!;
      for (let j = i + 1; j < nodeIds.length; j++) {
        const b = nodeIds[j];
        const pb = positions.get(b)!;
        const dx = pa.x - pb.x;
        const dy = pa.y - pb.y;
        const distSq = dx * dx + dy * dy + 0.01;
        const force = repulsionStrength / distSq;
        const dist = Math.sqrt(distSq);
        const fx = (dx / dist) * force;
        const fy = (dy / dist) * force;
        va.vx += fx;
        va.vy += fy;
        const vb = velocities.get(b)!;
        vb.vx -= fx;
        vb.vy -= fy;
      }
    }

    // Springs pull connected nodes toward `springLength` apart.
    for (const edge of edges) {
      const pa = positions.get(edge.source);
      const pb = positions.get(edge.target);
      if (!pa || !pb) continue;
      const dx = pb.x - pa.x;
      const dy = pb.y - pa.y;
      const dist = Math.sqrt(dx * dx + dy * dy) + 0.01;
      const force = (dist - springLength) * springStrength;
      const fx = (dx / dist) * force;
      const fy = (dy / dist) * force;
      velocities.get(edge.source)!.vx += fx;
      velocities.get(edge.source)!.vy += fy;
      velocities.get(edge.target)!.vx -= fx;
      velocities.get(edge.target)!.vy -= fy;
    }

    // Gentle pull toward the centre so the graph stays in frame.
    for (const id of nodeIds) {
      const p = positions.get(id)!;
      const v = velocities.get(id)!;
      v.vx += (cx - p.x) * centerStrength;
      v.vy += (cy - p.y) * centerStrength;
    }

    // Integrate with damping, then clamp to the viewBox.
    for (const id of nodeIds) {
      const p = positions.get(id)!;
      const v = velocities.get(id)!;
      v.vx *= damping;
      v.vy *= damping;
      p.x = Math.max(MAX_RADIUS, Math.min(WIDTH - MAX_RADIUS, p.x + v.vx));
      p.y = Math.max(MAX_RADIUS, Math.min(HEIGHT - MAX_RADIUS, p.y + v.vy));
    }
  }

  return positions;
}

function radiusForDegree(degree: number): number {
  return Math.min(MAX_RADIUS, BASE_RADIUS + Math.sqrt(degree) * RADIUS_PER_LINK);
}

export function GraphPage() {
  const graph = useQuery({
    queryKey: ['graph'],
    queryFn: async () => {
      const ds = await getDataSource();
      return ds.getGraph();
    },
    staleTime: 5 * 60_000,
  });

  const layoutData = useMemo(() => {
    if (!graph.data) return null;
    const degree = new Map<number, number>();
    for (const node of graph.data.nodes) degree.set(node.id, 0);
    for (const edge of graph.data.edges) {
      degree.set(edge.source, (degree.get(edge.source) ?? 0) + 1);
      degree.set(edge.target, (degree.get(edge.target) ?? 0) + 1);
    }
    const ids = graph.data.nodes.map((n) => n.id);
    const positions = layout(
      ids,
      graph.data.edges.map((e) => ({ source: e.source, target: e.target })),
    );
    return {
      nodes: graph.data.nodes,
      edges: graph.data.edges,
      degree,
      positions,
    };
  }, [graph.data]);

  if (graph.isPending) {
    return (
      <section>
        <h1 className="font-display text-3xl sm:text-4xl">Link graph</h1>
        <div className="mt-10">
          <LoadingState />
        </div>
      </section>
    );
  }
  if (graph.isError) {
    return (
      <section>
        <h1 className="font-display text-3xl sm:text-4xl">Link graph</h1>
        <div className="mt-10">
          <ErrorState message={graph.error.message} />
        </div>
      </section>
    );
  }
  if (!graph.data.nodes.length) {
    return (
      <section>
        <h1 className="font-display text-3xl sm:text-4xl">Link graph</h1>
        <p className="mt-6 font-mono text-muted">
          The garden has no pages yet.
        </p>
      </section>
    );
  }

  return (
    <section>
      <h1 className="font-display text-3xl sm:text-4xl">Link graph</h1>
      <p className="mt-5 max-w-xl text-lg text-muted">
        Every page in the garden. Bigger nodes are more connected.
      </p>
      <div className="mt-8 border-3 border-border bg-surface p-4 shadow-brutal">
        <p id="graph-description" className="sr-only">
          {layoutData
            ? `${layoutData.nodes.length} pages and ${layoutData.edges.length} links between them. Each node links to its page; use the page list or search to explore instead.`
            : 'Loading the link graph.'}
        </p>
        <svg
          viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
          aria-describedby="graph-description"
          className="block h-auto w-full"
          data-testid="graph-svg"
        >
          <g>
            {layoutData?.edges.map((edge, index) => {
              const a = layoutData.positions.get(edge.source);
              const b = layoutData.positions.get(edge.target);
              if (!a || !b) return null;
              return (
                <line
                  key={`${edge.source}-${edge.target}-${index}`}
                  x1={a.x}
                  y1={a.y}
                  x2={b.x}
                  y2={b.y}
                  stroke={colorForRel(edge.rel)}
                  strokeWidth={2}
                  strokeOpacity={0.6}
                />
              );
            })}
          </g>
          <g>
            {layoutData?.nodes.map((node) => {
              const pos = layoutData.positions.get(node.id);
              if (!pos) return null;
              const r = radiusForDegree(layoutData.degree.get(node.id) ?? 0);
              return (
                <Link
                  key={node.id}
                  to="/p/$slug"
                  params={{ slug: node.slug }}
                  aria-label={node.title || node.slug}
                  className="focus-ring cursor-pointer"
                  data-testid={`graph-node-${node.slug}`}
                >
                  <circle
                    cx={pos.x}
                    cy={pos.y}
                    r={r}
                    fill="var(--color-surface)"
                    stroke="var(--color-border)"
                    strokeWidth={3}
                  />
                  <text
                    x={pos.x + r + 6}
                    y={pos.y + 4}
                    fontFamily="var(--font-mono)"
                    fontSize={12}
                    fill="var(--color-text)"
                  >
                    {node.title || node.slug}
                  </text>
                </Link>
              );
            })}
          </g>
        </svg>
      </div>
      <EdgeLegend />
    </section>
  );
}

function EdgeLegend() {
  const rels = Object.keys(REL_COLORS);
  if (rels.length === 0) return null;
  return (
    <ul className="mt-6 flex flex-wrap gap-4 font-mono text-xs uppercase text-muted">
      {rels.map((rel) => (
        <li key={rel} className="flex items-center gap-2">
          <span
            aria-hidden
            className="inline-block h-2 w-6 border-2 border-border"
            style={{ backgroundColor: colorForRel(rel) }}
          />
          {rel}
        </li>
      ))}
    </ul>
  );
}
