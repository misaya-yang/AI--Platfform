import { motion } from "framer-motion";
import { Bot, Sparkles } from "lucide-react";
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
          className="h-24 w-24 rounded-3xl bg-gradient-to-br from-violet-100 via-purple-100 to-fuchsia-100 dark:from-violet-900/30 dark:via-purple-900/30 dark:to-fuchsia-900/30 flex items-center justify-center"
          animate={{ rotate: [0, 2, -2, 0] }}
          transition={{ duration: 4, repeat: Infinity, ease: "easeInOut" }}
        >
          <Bot className="h-12 w-12 text-violet-600 dark:text-violet-400" />
        </motion.div>
        <motion.div
          className="absolute -top-1 -right-1 h-5 w-5 rounded-full bg-gradient-to-br from-emerald-400 to-teal-500 shadow-lg shadow-emerald-500/30"
          animate={{ scale: [1, 1.2, 1] }}
          transition={{ duration: 2, repeat: Infinity }}
        />
      </div>

      <h2 className="text-2xl font-bold text-slate-800 dark:text-slate-100 mb-2">
        {t("assistant.welcomeTitle", "How can I help you today?")}
      </h2>
      <p className="text-slate-500 dark:text-slate-400 max-w-md text-sm leading-relaxed mb-8">
        {t(
          "assistant.welcomeDesc",
          "Select a model and knowledge bases, then send a message to begin."
        )}
      </p>
    </motion.div>
  );
}
