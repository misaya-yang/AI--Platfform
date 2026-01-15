/* eslint-disable react-refresh/only-export-components */
import * as React from "react";
import { Slot } from "@radix-ui/react-slot";
import { cva, type VariantProps } from "class-variance-authority";

import { cn } from "@/lib/utils";

const buttonVariants = cva(
  "group relative inline-flex items-center justify-center whitespace-nowrap rounded-xl text-sm font-medium transition-all duration-300 ease-out focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/70 focus-visible:ring-offset-2 disabled:pointer-events-none disabled:opacity-50 ring-offset-background overflow-hidden",
  {
    variants: {
      variant: {
        // Premium gradient primary button with glow
        default: [
          "text-white",
          "bg-[linear-gradient(135deg,hsl(var(--primary))_0%,#0ea5e9_50%,hsl(var(--primary))_100%)]",
          "bg-[length:200%_200%] bg-[position:0%_0%]",
          "shadow-[0_4px_16px_-2px_rgba(59,130,246,0.35),inset_0_1px_0_rgba(255,255,255,0.15)]",
          "hover:bg-[position:100%_100%]",
          "hover:shadow-[0_8px_24px_-4px_rgba(59,130,246,0.45),inset_0_1px_0_rgba(255,255,255,0.2)]",
          "hover:-translate-y-0.5 hover:scale-[1.02]",
          "active:translate-y-0 active:scale-100",
          "before:absolute before:inset-0 before:bg-[linear-gradient(to_right,transparent_0%,rgba(255,255,255,0.1)_50%,transparent_100%)]",
          "before:translate-x-[-100%] before:transition-transform before:duration-500",
          "hover:before:translate-x-[100%]",
        ].join(" "),
        // Refined secondary button with subtle depth
        secondary: [
          "bg-secondary text-secondary-foreground",
          "border border-border/60",
          "shadow-[0_1px_3px_rgba(0,0,0,0.05),inset_0_1px_0_rgba(255,255,255,0.6)]",
          "dark:shadow-[0_1px_3px_rgba(0,0,0,0.2),inset_0_1px_0_rgba(255,255,255,0.05)]",
          "hover:bg-secondary/70 hover:border-border",
          "hover:shadow-[0_3px_8px_rgba(0,0,0,0.08)]",
          "dark:hover:shadow-[0_3px_8px_rgba(0,0,0,0.3)]",
          "active:bg-secondary/90",
        ].join(" "),
        // Elegant outline button
        outline: [
          "border border-input bg-background/80 backdrop-blur-sm",
          "shadow-[0_1px_2px_rgba(0,0,0,0.04)]",
          "hover:bg-accent/50 hover:text-accent-foreground",
          "hover:border-primary/40 hover:shadow-[0_2px_8px_rgba(59,130,246,0.1)]",
          "active:bg-accent/70",
        ].join(" "),
        // Refined ghost button
        ghost: [
          "hover:bg-accent/60 hover:text-accent-foreground",
          "active:bg-accent/80",
        ].join(" "),
        // Premium destructive button with glow
        destructive: [
          "text-white",
          "bg-[linear-gradient(135deg,#ef4444_0%,#f87171_50%,#ef4444_100%)]",
          "bg-[length:200%_200%] bg-[position:0%_0%]",
          "shadow-[0_4px_16px_-2px_rgba(239,68,68,0.35),inset_0_1px_0_rgba(255,255,255,0.15)]",
          "hover:bg-[position:100%_100%]",
          "hover:shadow-[0_8px_24px_-4px_rgba(239,68,68,0.45),inset_0_1px_0_rgba(255,255,255,0.2)]",
          "hover:-translate-y-0.5 hover:scale-[1.02]",
          "active:translate-y-0 active:scale-100",
          "before:absolute before:inset-0 before:bg-[linear-gradient(to_right,transparent_0%,rgba(255,255,255,0.1)_50%,transparent_100%)]",
          "before:translate-x-[-100%] before:transition-transform before:duration-500",
          "hover:before:translate-x-[100%]",
        ].join(" "),
        // Link style with smooth underline
        link: [
          "text-primary underline-offset-4",
          "hover:underline",
          "hover:text-primary/80",
        ].join(" "),
      },
      size: {
        default: "h-10 px-5 py-2",
        sm: "h-9 rounded-lg px-3.5 text-xs",
        lg: "h-11 rounded-xl px-8 text-base",
        icon: "h-10 w-10 rounded-xl",
      },
    },
    defaultVariants: {
      variant: "default",
      size: "default",
    },
  }
);

export interface ButtonProps
  extends React.ButtonHTMLAttributes<HTMLButtonElement>,
    VariantProps<typeof buttonVariants> {
  asChild?: boolean;
}

const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(
  ({ className, variant, size, asChild = false, ...props }, ref) => {
    const Comp = asChild ? Slot : "button";
    return (
      <Comp
        className={cn(buttonVariants({ variant, size, className }))}
        ref={ref}
        {...props}
      />
    );
  }
);
Button.displayName = "Button";

export { Button, buttonVariants };
