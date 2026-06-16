import { motion } from "framer-motion";
import { Bot } from "lucide-react";
import { useTranslation } from "react-i18next";

export function WelcomeScreen() {
  const { t } = useTranslation();

  return (
    <motion.div
      initial={{ opacity: 0, y: 20 }}
      animate={{ opacity: 1, y: 0 }}
      className="flex flex-col items-center justify-center min-h-[50vh] text-center"
    >
      {/* Welcome hero */}
      <div className="relative mb-8">
        <motion.div
          className="h-20 w-20 rounded-2xl bg-primary/10 dark:bg-primary/15 flex items-center justify-center"
          animate={{ rotate: [0, 2, -2, 0] }}
          transition={{ duration: 4, repeat: Infinity, ease: "easeInOut" }}
        >
          <Bot className="h-10 w-10 text-primary" />
        </motion.div>
        <motion.div
          className="absolute -top-1 -right-1 h-4 w-4 rounded-full bg-emerald-500 shadow-xs"
          animate={{ scale: [1, 1.2, 1] }}
          transition={{ duration: 2, repeat: Infinity }}
        />
      </div>

      <h2 className="text-2xl font-bold text-foreground mb-2">
        {t("assistant.welcomeTitle", "How can I help you today?")}
      </h2>
      <p className="text-muted-foreground max-w-md text-sm leading-relaxed mb-8">
        {t(
          "assistant.welcomeDesc",
          "Select a model and knowledge bases, then send a message to begin."
        )}
      </p>
    </motion.div>
  );
}
