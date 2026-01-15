import * as React from "react";
import * as SelectPrimitive from "@radix-ui/react-select";
import { Check, ChevronDown, ChevronUp } from "lucide-react";

import { cn } from "@/lib/utils";

const Select = SelectPrimitive.Root;
const SelectValue = SelectPrimitive.Value;

const SelectTrigger = React.forwardRef<
  React.ElementRef<typeof SelectPrimitive.Trigger>,
  React.ComponentPropsWithoutRef<typeof SelectPrimitive.Trigger>
>(({ className, children, ...props }, ref) => (
  <SelectPrimitive.Trigger
    ref={ref}
    className={cn(
      // Base styles with solid background
      "group flex h-10 w-full items-center justify-between rounded-xl px-3 py-2 text-sm",
      // Solid opaque background - explicitly white/dark
      "bg-white dark:bg-slate-900",
      // Border with subtle shadow for depth
      "border border-slate-200 dark:border-slate-700",
      "shadow-[0_1px_2px_rgba(0,0,0,0.04),inset_0_1px_0_rgba(255,255,255,0.8)]",
      "dark:shadow-[0_1px_2px_rgba(0,0,0,0.2),inset_0_1px_0_rgba(255,255,255,0.03)]",
      // Focus and hover states
      "ring-offset-background transition-all duration-200",
      "hover:border-slate-300 dark:hover:border-slate-600",
      "hover:shadow-[0_2px_4px_rgba(0,0,0,0.06)]",
      "focus:outline-none focus:ring-2 focus:ring-ring/50 focus:ring-offset-2",
      "focus:border-primary/50",
      // Placeholder and disabled
      "placeholder:text-muted-foreground",
      "disabled:cursor-not-allowed disabled:opacity-50",
      className
    )}
    {...props}
  >
    {children}
    <SelectPrimitive.Icon asChild>
      <ChevronDown className="h-4 w-4 text-slate-400 transition-transform duration-200 group-data-[state=open]:rotate-180" />
    </SelectPrimitive.Icon>
  </SelectPrimitive.Trigger>
));
SelectTrigger.displayName = SelectPrimitive.Trigger.displayName;

const SelectContent = React.forwardRef<
  React.ElementRef<typeof SelectPrimitive.Content>,
  React.ComponentPropsWithoutRef<typeof SelectPrimitive.Content>
>(({ className, children, position = "popper", ...props }, ref) => (
  <SelectPrimitive.Portal>
    <SelectPrimitive.Content
      ref={ref}
      className={cn(
        // Solid opaque background - NOT transparent
        "relative max-h-96 min-w-[8rem] overflow-hidden rounded-xl",
        "bg-white dark:bg-slate-900",
        // Border with premium shadow
        "border border-slate-200/80 dark:border-slate-700/80",
        "shadow-[0_10px_38px_-10px_rgba(22,23,24,0.35),0_10px_20px_-15px_rgba(22,23,24,0.2)]",
        "dark:shadow-[0_10px_38px_-10px_rgba(0,0,0,0.5),0_10px_20px_-15px_rgba(0,0,0,0.4)]",
        // Subtle ring for extra definition
        "ring-1 ring-slate-900/5 dark:ring-white/10",
        // Text color
        "text-slate-900 dark:text-slate-100",
        // Animations
        "data-[state=open]:animate-in data-[state=closed]:animate-out",
        "data-[state=closed]:fade-out-0 data-[state=open]:fade-in-0",
        "data-[state=closed]:zoom-out-95 data-[state=open]:zoom-in-95",
        "data-[side=bottom]:slide-in-from-top-2 data-[side=left]:slide-in-from-right-2",
        "data-[side=right]:slide-in-from-left-2 data-[side=top]:slide-in-from-bottom-2",
        position === "popper" &&
          "data-[side=bottom]:translate-y-1 data-[side=left]:-translate-x-1 data-[side=right]:translate-x-1 data-[side=top]:-translate-y-1",
        className
      )}
      style={{ zIndex: 9999 }}
      position={position}
      {...props}
    >
      <SelectPrimitive.ScrollUpButton className="flex cursor-default items-center justify-center py-1.5 text-slate-400 hover:text-slate-600 dark:hover:text-slate-300">
        <ChevronUp className="h-4 w-4" />
      </SelectPrimitive.ScrollUpButton>
      <SelectPrimitive.Viewport
        className={cn(
          "p-1.5",
          position === "popper" &&
            "h-[var(--radix-select-trigger-height)] w-full min-w-[var(--radix-select-trigger-width)]"
        )}
      >
        {children}
      </SelectPrimitive.Viewport>
      <SelectPrimitive.ScrollDownButton className="flex cursor-default items-center justify-center py-1.5 text-slate-400 hover:text-slate-600 dark:hover:text-slate-300">
        <ChevronDown className="h-4 w-4" />
      </SelectPrimitive.ScrollDownButton>
    </SelectPrimitive.Content>
  </SelectPrimitive.Portal>
));
SelectContent.displayName = SelectPrimitive.Content.displayName;

const SelectItem = React.forwardRef<
  React.ElementRef<typeof SelectPrimitive.Item>,
  React.ComponentPropsWithoutRef<typeof SelectPrimitive.Item>
>(({ className, children, ...props }, ref) => (
  <SelectPrimitive.Item
    ref={ref}
    className={cn(
      // Base styles
      "relative flex w-full cursor-pointer select-none items-center rounded-lg py-2 pl-3 pr-9 text-sm outline-none",
      // Hover state with subtle gradient
      "transition-all duration-150",
      "hover:bg-slate-100 dark:hover:bg-slate-800",
      // Focus/highlighted state with primary color hint
      "focus:bg-gradient-to-r focus:from-primary/10 focus:to-sky-500/5",
      "focus:text-slate-900 dark:focus:text-slate-100",
      // Selected indicator background
      "data-[state=checked]:bg-primary/5 data-[state=checked]:text-primary",
      // Disabled
      "data-[disabled]:pointer-events-none data-[disabled]:opacity-50",
      className
    )}
    {...props}
  >
    <span className="absolute right-2.5 flex h-4 w-4 items-center justify-center">
      <SelectPrimitive.ItemIndicator>
        <Check className="h-4 w-4 text-primary" />
      </SelectPrimitive.ItemIndicator>
    </span>
    <SelectPrimitive.ItemText>{children}</SelectPrimitive.ItemText>
  </SelectPrimitive.Item>
));
SelectItem.displayName = SelectPrimitive.Item.displayName;

export {
  Select,
  SelectValue,
  SelectTrigger,
  SelectContent,
  SelectItem,
};

