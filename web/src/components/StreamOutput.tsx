import { useState, useEffect, useRef } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

interface StreamOutputProps {
  text: string;
  /** Enable typing animation for text that arrives all at once */
  enableTypingEffect?: boolean;
  /** Characters per frame for typing effect (higher = faster) */
  typingSpeed?: number;
}

export function StreamOutput({ 
  text, 
  enableTypingEffect = true,
  typingSpeed = 8 
}: StreamOutputProps) {
  const [displayedText, setDisplayedText] = useState("");
  const prevTextRef = useRef("");
  const animationFrameRef = useRef<number>();
  const targetTextRef = useRef(text);
  
  useEffect(() => {
    targetTextRef.current = text;
    
    // If text was cleared, reset immediately
    if (!text) {
      setDisplayedText("");
      prevTextRef.current = "";
      return;
    }
    
    // If typing effect is disabled, show text immediately
    if (!enableTypingEffect) {
      setDisplayedText(text);
      prevTextRef.current = text;
      return;
    }
    
    // Calculate how much new text was added
    const prevText = prevTextRef.current;
    
    // If this is incremental (streaming), just append directly
    if (text.startsWith(prevText) && text.length > prevText.length) {
      const newPart = text.slice(prevText.length);
      // For small increments (real streaming), add immediately
      if (newPart.length <= typingSpeed * 2) {
        setDisplayedText(text);
        prevTextRef.current = text;
        return;
      }
    }
    
    // For large jumps (all text at once), animate typing
    const animateTyping = () => {
      setDisplayedText(current => {
        const target = targetTextRef.current;
        if (current.length >= target.length) {
          prevTextRef.current = target;
          return target;
        }
        
        // Add characters per frame
        const nextLength = Math.min(current.length + typingSpeed, target.length);
        const nextText = target.slice(0, nextLength);
        
        // Continue animation if not complete
        if (nextLength < target.length) {
          animationFrameRef.current = requestAnimationFrame(animateTyping);
        } else {
          prevTextRef.current = target;
        }
        
        return nextText;
      });
    };
    
    // Cancel any existing animation
    if (animationFrameRef.current) {
      cancelAnimationFrame(animationFrameRef.current);
    }
    
    // Start animation
    animationFrameRef.current = requestAnimationFrame(animateTyping);
    
    return () => {
      if (animationFrameRef.current) {
        cancelAnimationFrame(animationFrameRef.current);
      }
    };
  }, [text, enableTypingEffect, typingSpeed]);
  
  // Cleanup on unmount
  useEffect(() => {
    return () => {
      if (animationFrameRef.current) {
        cancelAnimationFrame(animationFrameRef.current);
      }
    };
  }, []);

  return (
    <div className="prose prose-sm max-w-none dark:prose-invert">
      <ReactMarkdown remarkPlugins={[remarkGfm]}>{displayedText || text}</ReactMarkdown>
    </div>
  );
}
