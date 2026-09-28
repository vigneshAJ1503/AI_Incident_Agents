"use client";

import { animate, motion, useInView, useReducedMotion, type Variants } from "framer-motion";
import { useEffect, useRef, useState } from "react";
import type * as React from "react";

export const listVariants: Variants = {
  hidden: {},
  show: { transition: { staggerChildren: 0.05, delayChildren: 0.02 } },
};

export const itemVariants: Variants = {
  hidden: { opacity: 0, y: 8 },
  show: { opacity: 1, y: 0, transition: { type: "spring", stiffness: 380, damping: 32 } },
};

/** Staggered entrance for a group of cards/rows. */
export function Stagger({
  children,
  className,
  as = "div",
}: {
  children: React.ReactNode;
  className?: string;
  as?: "div" | "ul";
}) {
  const Comp = as === "ul" ? motion.ul : motion.div;
  return (
    <Comp className={className} variants={listVariants} initial="hidden" animate="show">
      {children}
    </Comp>
  );
}

export function StaggerItem({
  children,
  className,
  as = "div",
}: {
  children: React.ReactNode;
  className?: string;
  as?: "div" | "li";
}) {
  const Comp = as === "li" ? motion.li : motion.div;
  return (
    <Comp className={className} variants={itemVariants}>
      {children}
    </Comp>
  );
}

/** Animated number ticker for KPIs (jumps straight to the value under prefers-reduced-motion). */
export function NumberTicker({
  value,
  format = (n) => Math.round(n).toString(),
  className,
}: {
  value: number;
  format?: (n: number) => string;
  className?: string;
}) {
  const ref = useRef<HTMLSpanElement>(null);
  const inView = useInView(ref, { once: true });
  const reduce = useReducedMotion();
  const [animated, setShown] = useState(0);
  const shown = reduce ? value : animated;

  useEffect(() => {
    if (!inView || reduce) return;
    const controls = animate(0, value, {
      duration: 0.9,
      ease: [0.16, 1, 0.3, 1],
      onUpdate: setShown,
    });
    return () => controls.stop();
  }, [inView, reduce, value]);

  return (
    <span ref={ref} className={className}>
      <span aria-hidden>{format(shown)}</span>
      <span className="sr-only">{format(value)}</span>
    </span>
  );
}
