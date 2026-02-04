import { useState, useEffect, useMemo } from "react";
import { useParams, useNavigate, Link } from "react-router-dom";
import { motion, AnimatePresence } from "framer-motion";
import { useTranslation } from "react-i18next";
import {
  ArrowLeftOutlined,
  UserOutlined,
  MailOutlined,
  TeamOutlined,
  SafetyCertificateOutlined,
  KeyOutlined,
  CheckOutlined,
  LoadingOutlined,
  ExclamationCircleOutlined,
  DownOutlined,
} from "@ant-design/icons";
import { message, Spin, Tooltip } from "antd";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Badge } from "@/components/ui/badge";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { useAuthStore } from "@/store/useAuthStore";
import { useAppStore } from "@/store/useAppStore";
import {
  getUser,
  updateUser,
  listRoles,
  listPermissions,
} from "@/api/users";
import type { UserResponse, RoleResponse, PermissionResponse } from "@/api/users";
import { colors } from "@/theme/themeConfig";

const buildDepartmentOptions = (t: (key: string, options?: Record<string, unknown>) => string) => [
  { value: "cs", label: t("user.departments.cs"), color: "#10b981" },
  { value: "sales", label: t("user.departments.sales"), color: "#f59e0b" },
  { value: "tech", label: t("user.departments.tech"), color: "#3b82f6" },
  { value: "admin", label: t("user.departments.admin"), color: "#8b5cf6" },
];

const buildStatusOptions = (t: (key: string, options?: Record<string, unknown>) => string) => [
  { value: "active", label: t("users.status.active"), color: "#10b981" },
  { value: "disabled", label: t("users.status.disabled"), color: "#ef4444" },
];

const buildCategoryMeta = (t: (key: string, options?: Record<string, unknown>) => string) => ({
  console: { icon: "🖥️", color: "#3b82f6", label: t("users.permissions.categories.console") },
  conversation: { icon: "💬", color: "#10b981", label: t("users.permissions.categories.conversation") },
  knowledge: { icon: "📚", color: "#f59e0b", label: t("users.permissions.categories.knowledge") },
  user: { icon: "👤", color: "#8b5cf6", label: t("users.permissions.categories.user") },
  role: { icon: "🔐", color: "#ec4899", label: t("users.permissions.categories.role") },
  admin: { icon: "⚙️", color: "#ef4444", label: t("users.permissions.categories.admin") },
});

export function UserEditPage() {
  const { t, i18n } = useTranslation();
  const { userId } = useParams<{ userId: string }>();
  const navigate = useNavigate();
  const { hasPermission } = useAuthStore();
  const { darkMode } = useAppStore();
  const departmentOptions = useMemo(() => buildDepartmentOptions(t), [t, i18n.language]);
  const statusOptions = useMemo(() => buildStatusOptions(t), [t, i18n.language]);
  const categoryMeta = useMemo(() => buildCategoryMeta(t), [t, i18n.language]);

  // Data states
  const [user, setUser] = useState<UserResponse | null>(null);
  const [roles, setRoles] = useState<RoleResponse[]>([]);
  const [permissions, setPermissions] = useState<PermissionResponse[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [isSaving, setIsSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Form states
  const [formDisplayName, setFormDisplayName] = useState("");
  const [formDepartment, setFormDepartment] = useState<string>("");
  const [formStatus, setFormStatus] = useState("active");
  const [formRoles, setFormRoles] = useState<string[]>([]);
  const [formExtraPermissions, setFormExtraPermissions] = useState<string[]>([]);

  // UI states
  const [expandedCategories, setExpandedCategories] = useState<Set<string>>(new Set(["console"]));
  const [hasChanges, setHasChanges] = useState(false);

  // Permissions check
  const canEdit = hasPermission("user:edit");
  const canViewRoles = hasPermission("role:list");

  // Load data
  useEffect(() => {
    if (!userId) return;

    const loadData = async () => {
      setIsLoading(true);
      setError(null);
      try {
        const [userData, rolesData, permsData] = await Promise.all([
          getUser(userId),
          canViewRoles ? listRoles() : Promise.resolve({ roles: [] }),
          canViewRoles ? listPermissions() : Promise.resolve({ permissions: [] }),
        ]);

        setUser(userData);
        setRoles(rolesData.roles);
        setPermissions(permsData.permissions);

        // Initialize form
        setFormDisplayName(userData.display_name || "");
        setFormDepartment(userData.department || "");
        setFormStatus(userData.status);
        setFormRoles(userData.roles);
        setFormExtraPermissions(userData.extra_permissions || []);
      } catch (err) {
        console.error("Failed to load user:", err);
        setError(t("users.edit.loadFailed"));
      } finally {
        setIsLoading(false);
      }
    };

    loadData();
  }, [userId, canViewRoles]);

  // Track changes
  useEffect(() => {
    if (!user) return;
    const changed =
      formDisplayName !== (user.display_name || "") ||
      formDepartment !== (user.department || "") ||
      formStatus !== user.status ||
      JSON.stringify([...formRoles].sort()) !== JSON.stringify([...user.roles].sort()) ||
      JSON.stringify([...formExtraPermissions].sort()) !== JSON.stringify([...(user.extra_permissions || [])].sort());
    setHasChanges(changed);
  }, [user, formDisplayName, formDepartment, formStatus, formRoles, formExtraPermissions]);

  // Group permissions by category
  const permissionsByCategory = useMemo(() => {
    const grouped: Record<string, PermissionResponse[]> = {};
    permissions.forEach((perm) => {
      const cat = perm.category || "other";
      if (!grouped[cat]) grouped[cat] = [];
      grouped[cat].push(perm);
    });
    return grouped;
  }, [permissions]);

  // Get permissions from selected roles
  const rolePermissions = useMemo(() => {
    const perms = new Set<string>();
    const selectedRoles = new Set(formRoles);
    roles.forEach((role) => {
      if (selectedRoles.has(role.role_name)) {
        role.permissions.forEach((p) => perms.add(p));
      }
    });
    return perms;
  }, [formRoles, roles]);

  // Toggle category expansion
  const toggleCategory = (category: string) => {
    setExpandedCategories((prev) => {
      const next = new Set(prev);
      if (next.has(category)) {
        next.delete(category);
      } else {
        next.add(category);
      }
      return next;
    });
  };

  // Handle save
  const handleSave = async () => {
    if (!userId || !canEdit) return;
    setIsSaving(true);
    try {
      await updateUser(userId, {
        display_name: formDisplayName,
        department: formDepartment || undefined,
        status: formStatus,
        roles: formRoles,
        extra_permissions: formExtraPermissions,
      });
      message.success(t("users.edit.saved"));
      // Reload user data
      const userData = await getUser(userId);
      setUser(userData);
      setHasChanges(false);
    } catch (err: unknown) {
      const axiosError = err as { response?: { data?: { detail?: string } } };
      message.error(axiosError.response?.data?.detail || t("users.edit.saveFailed"));
    } finally {
      setIsSaving(false);
    }
  };

  if (isLoading) {
    return (
      <div className="flex items-center justify-center min-h-[400px]">
        <Spin size="large" />
      </div>
    );
  }

  if (error || !user) {
    return (
      <div className="flex flex-col items-center justify-center min-h-[400px] gap-4">
        <ExclamationCircleOutlined style={{ fontSize: 48, color: '#ff4d4f' }} />
        <p className="text-lg text-muted-foreground">{error || t("users.edit.notFound")}</p>
        <Button variant="outline" onClick={() => navigate("/users")}>
          {t("users.edit.backToList")}
        </Button>
      </div>
    );
  }

  const dept = departmentOptions.find((d) => d.value === user.department);
  const statusOpt = statusOptions.find((s) => s.value === user.status);

  return (
    <div className="user-edit-page pb-24">
      {/* Breadcrumb */}
      <div className="flex items-center gap-2 mb-6 text-sm">
        <Link
          to="/users"
          className="flex items-center gap-1 text-muted-foreground hover:text-foreground transition-colors"
        >
          <ArrowLeftOutlined />
          <span>{t("users.title")}</span>
        </Link>
        <span className="text-muted-foreground">/</span>
        <span className="text-foreground font-medium">{t("users.editUser")}</span>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-[320px_1fr] gap-6">
        {/* Left sidebar - User profile card */}
        <div className="space-y-4">
          <motion.div
            initial={{ opacity: 0, y: 20 }}
            animate={{ opacity: 1, y: 0 }}
            className="user-profile-card rounded-2xl p-6"
            style={{
              background: darkMode
                ? `linear-gradient(145deg, ${colors.neutral[800]}, ${colors.neutral[900]})`
                : "linear-gradient(145deg, #ffffff, #f8fafc)",
              border: `1px solid ${darkMode ? colors.neutral[700] : colors.neutral[200]}`,
              boxShadow: darkMode
                ? "0 4px 24px rgba(0,0,0,0.3)"
                : "0 4px 24px rgba(0,0,0,0.06)",
            }}
          >
            {/* Avatar */}
            <div className="flex flex-col items-center mb-6">
              <div
                className="w-20 h-20 rounded-2xl flex items-center justify-center mb-4"
                style={{
                  background: `linear-gradient(135deg, ${colors.primary[400]}, #22d3ee)`,
                  boxShadow: `0 8px 24px ${colors.primary[500]}40`,
                }}
              >
                <UserOutlined style={{ fontSize: 36, color: "#fff" }} />
              </div>
              <h2 className="text-xl font-semibold">{user.display_name || user.email}</h2>
              <p className="text-sm text-muted-foreground">{user.email}</p>
            </div>

            {/* User info */}
            <div className="space-y-4">
              <div className="flex items-center gap-3 p-3 rounded-xl" style={{
                background: darkMode ? "rgba(255,255,255,0.03)" : "rgba(0,0,0,0.02)",
              }}>
                <MailOutlined style={{ color: colors.primary[500] }} />
                <div className="flex-1 min-w-0">
                  <p className="text-xs text-muted-foreground">{t("users.fields.email")}</p>
                  <p className="text-sm font-medium truncate">{user.email}</p>
                </div>
              </div>

              <div className="flex items-center gap-3 p-3 rounded-xl" style={{
                background: darkMode ? "rgba(255,255,255,0.03)" : "rgba(0,0,0,0.02)",
              }}>
                <TeamOutlined style={{ color: dept?.color || colors.neutral[500] }} />
                <div className="flex-1 min-w-0">
                  <p className="text-xs text-muted-foreground">{t("users.fields.department")}</p>
                  <p className="text-sm font-medium">{dept?.label || t("common.notSet")}</p>
                </div>
              </div>

              <div className="flex items-center gap-3 p-3 rounded-xl" style={{
                background: darkMode ? "rgba(255,255,255,0.03)" : "rgba(0,0,0,0.02)",
              }}>
                <SafetyCertificateOutlined style={{ color: statusOpt?.color }} />
                <div className="flex-1 min-w-0">
                  <p className="text-xs text-muted-foreground">{t("users.fields.status")}</p>
                  <Badge
                    variant={user.status === "active" ? "default" : "destructive"}
                    className="mt-1"
                  >
                    {statusOpt?.label}
                  </Badge>
                </div>
              </div>

              <div className="flex items-center gap-3 p-3 rounded-xl" style={{
                background: darkMode ? "rgba(255,255,255,0.03)" : "rgba(0,0,0,0.02)",
              }}>
                <KeyOutlined style={{ color: "#a855f7" }} />
                <div className="flex-1 min-w-0">
                  <p className="text-xs text-muted-foreground">{t("users.fields.roles")}</p>
                  <div className="flex flex-wrap gap-1 mt-1">
                    {user.roles.map((role) => (
                      <Badge key={role} variant="secondary" className="text-xs">
                        {role}
                      </Badge>
                    ))}
                  </div>
                </div>
              </div>
            </div>

            {/* Timestamps */}
            <div className="mt-6 pt-4 border-t text-xs text-muted-foreground space-y-1" style={{
              borderColor: darkMode ? colors.neutral[700] : colors.neutral[200],
            }}>
              <p>{t("users.fields.createdAt")}: {user.created_at ? new Date(user.created_at).toLocaleString() : "-"}</p>
              <p>{t("users.fields.lastLogin")}: {user.last_login_at ? new Date(user.last_login_at).toLocaleString() : t("common.never")}</p>
            </div>
          </motion.div>
        </div>

        {/* Right content - Edit cards */}
        <div className="space-y-6">
          {/* Basic info card */}
          <motion.div
            initial={{ opacity: 0, y: 20 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ delay: 0.1 }}
            className="edit-card rounded-2xl p-6"
            style={{
              background: darkMode ? colors.neutral[800] : "#ffffff",
              border: `1px solid ${darkMode ? colors.neutral[700] : colors.neutral[200]}`,
            }}
          >
            <div className="flex items-center gap-3 mb-6">
              <div className="w-10 h-10 rounded-xl flex items-center justify-center" style={{
                background: `${colors.primary[500]}15`,
              }}>
                <UserOutlined style={{ fontSize: 18, color: colors.primary[500] }} />
              </div>
              <div>
                <h3 className="font-semibold">{t("users.edit.basicInfo.title")}</h3>
                <p className="text-xs text-muted-foreground">{t("users.edit.basicInfo.desc")}</p>
              </div>
            </div>

            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
              <div className="space-y-2">
                <Label>{t("users.fields.displayName")}</Label>
                <Input
                  value={formDisplayName}
                  onChange={(e) => setFormDisplayName(e.target.value)}
                  placeholder={t("users.edit.displayNamePlaceholder")}
                  disabled={!canEdit}
                />
              </div>

              <div className="space-y-2">
                <Label>{t("users.fields.department")}</Label>
                <Select value={formDepartment} onValueChange={setFormDepartment} disabled={!canEdit}>
                  <SelectTrigger>
                    <SelectValue placeholder={t("users.edit.departmentPlaceholder")} />
                  </SelectTrigger>
                  <SelectContent>
                    {departmentOptions.map((dept) => (
                      <SelectItem key={dept.value} value={dept.value}>
                        {dept.label}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>

              <div className="space-y-2">
                <Label>{t("users.fields.status")}</Label>
                <Select value={formStatus} onValueChange={setFormStatus} disabled={!canEdit}>
                  <SelectTrigger>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {statusOptions.map((opt) => (
                      <SelectItem key={opt.value} value={opt.value}>
                        {opt.label}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
            </div>
          </motion.div>

          {/* Roles card */}
          <motion.div
            initial={{ opacity: 0, y: 20 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ delay: 0.2 }}
            className="edit-card rounded-2xl p-6"
            style={{
              background: darkMode ? colors.neutral[800] : "#ffffff",
              border: `1px solid ${darkMode ? colors.neutral[700] : colors.neutral[200]}`,
            }}
          >
            <div className="flex items-center gap-3 mb-6">
              <div className="w-10 h-10 rounded-xl flex items-center justify-center" style={{
                background: "#a855f715",
              }}>
                <SafetyCertificateOutlined style={{ fontSize: 18, color: "#a855f7" }} />
              </div>
              <div>
                <h3 className="font-semibold">{t("users.edit.roles.title")}</h3>
                <p className="text-xs text-muted-foreground">{t("users.edit.roles.desc")}</p>
              </div>
            </div>

            {canViewRoles ? (
              <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                {roles.map((role) => {
                  const isSelected = formRoles.includes(role.role_name);
                  return (
                    <motion.div
                      key={role.role_name}
                      whileHover={{ scale: 1.01 }}
                      whileTap={{ scale: 0.99 }}
                      onClick={() => {
                        if (!canEdit) return;
                        if (isSelected) {
                          setFormRoles(formRoles.filter((r) => r !== role.role_name));
                        } else {
                          setFormRoles([...formRoles, role.role_name]);
                        }
                      }}
                      className="role-card p-4 rounded-xl cursor-pointer transition-all"
                      style={{
                        background: isSelected
                          ? darkMode
                            ? `${colors.primary[500]}20`
                            : `${colors.primary[500]}10`
                          : darkMode
                          ? "rgba(255,255,255,0.03)"
                          : "rgba(0,0,0,0.02)",
                        border: `2px solid ${isSelected ? colors.primary[500] : "transparent"}`,
                        opacity: canEdit ? 1 : 0.6,
                      }}
                    >
                      <div className="flex items-start gap-3">
                        <Checkbox
                          checked={isSelected}
                          disabled={!canEdit}
                          className="mt-0.5"
                        />
                        <div className="flex-1 min-w-0">
                          <div className="flex items-center gap-2">
                            <span className="font-medium">{role.role_name}</span>
                            {role.is_system && (
                              <Badge variant="outline" className="text-xs">{t("users.edit.roles.system")}</Badge>
                            )}
                          </div>
                          <p className="text-xs text-muted-foreground mt-1 line-clamp-2">
                            {role.description || t("users.edit.roles.noDescription")}
                          </p>
                          <p className="text-xs text-muted-foreground mt-2">
                            {t("users.edit.roles.permissionCount", { count: role.permissions.length })}
                          </p>
                        </div>
                      </div>
                    </motion.div>
                  );
                })}
              </div>
            ) : (
              <p className="text-sm text-muted-foreground">{t("users.edit.roles.noAccess")}</p>
            )}
          </motion.div>

          {/* Extra permissions card */}
          <motion.div
            initial={{ opacity: 0, y: 20 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ delay: 0.3 }}
            className="edit-card rounded-2xl p-6"
            style={{
              background: darkMode ? colors.neutral[800] : "#ffffff",
              border: `1px solid ${darkMode ? colors.neutral[700] : colors.neutral[200]}`,
            }}
          >
            <div className="flex items-center gap-3 mb-2">
              <div className="w-10 h-10 rounded-xl flex items-center justify-center" style={{
                background: "#06b6d415",
              }}>
                <KeyOutlined style={{ fontSize: 18, color: "#06b6d4" }} />
              </div>
              <div>
                <h3 className="font-semibold">{t("users.edit.extraPermissions.title")}</h3>
                <p className="text-xs text-muted-foreground">{t("users.edit.extraPermissions.desc")}</p>
              </div>
            </div>

            <div className="mb-4 p-3 rounded-lg text-xs" style={{
              background: darkMode ? "rgba(59, 130, 246, 0.1)" : "rgba(59, 130, 246, 0.05)",
              color: colors.primary[darkMode ? 400 : 600],
            }}>
              <strong>{t("users.edit.extraPermissions.tipLabel")}:</strong> {t("users.edit.extraPermissions.tipText")}
            </div>

            {canViewRoles ? (
              <div className="space-y-2">
                {Object.entries(permissionsByCategory).map(([category, perms]) => {
                  const meta = categoryMeta[category] || { icon: "📋", color: "#6b7280", label: category };
                  const isExpanded = expandedCategories.has(category);
                  const selectedInCategory = perms.filter((p) => formExtraPermissions.includes(p.permission_code)).length;

                  return (
                    <div key={category} className="permission-category rounded-xl overflow-hidden" style={{
                      border: `1px solid ${darkMode ? colors.neutral[700] : colors.neutral[200]}`,
                    }}>
                      {/* Category header */}
                      <div
                        onClick={() => toggleCategory(category)}
                        className="flex items-center justify-between p-4 cursor-pointer transition-colors"
                        style={{
                          background: darkMode ? "rgba(255,255,255,0.02)" : "rgba(0,0,0,0.01)",
                        }}
                      >
                        <div className="flex items-center gap-3">
                          <span className="text-lg">{meta.icon}</span>
                          <span className="font-medium">{meta.label}</span>
                          <span className="text-xs text-muted-foreground">
                            ({t("users.edit.extraPermissions.categoryCount", { count: perms.length })})
                          </span>
                          {selectedInCategory > 0 && (
                            <Badge variant="default" className="text-xs">
                              +{selectedInCategory}
                            </Badge>
                          )}
                        </div>
                        <motion.div
                          animate={{ rotate: isExpanded ? 0 : -90 }}
                          transition={{ duration: 0.2 }}
                        >
                          <DownOutlined style={{ fontSize: 12, color: colors.neutral[500] }} />
                        </motion.div>
                      </div>

                      {/* Category content */}
                      <AnimatePresence>
                        {isExpanded && (
                          <motion.div
                            initial={{ height: 0, opacity: 0 }}
                            animate={{ height: "auto", opacity: 1 }}
                            exit={{ height: 0, opacity: 0 }}
                            transition={{ duration: 0.2 }}
                            style={{ overflow: "hidden" }}
                          >
                            <div className="p-4 pt-0 grid grid-cols-1 md:grid-cols-2 gap-2">
                              {perms.map((perm) => {
                                const fromRole = rolePermissions.has(perm.permission_code);
                                const isSelected = formExtraPermissions.includes(perm.permission_code);

                                return (
                                  <Tooltip
                                    key={perm.permission_code}
                                    title={
                                      <div>
                                        <p><strong>{t("common.code")}:</strong> {perm.permission_code}</p>
                                        {perm.description && <p><strong>{t("common.description")}:</strong> {perm.description}</p>}
                                        {fromRole && <p className="text-yellow-400 mt-1">{t("users.edit.extraPermissions.fromRole")}</p>}
                                      </div>
                                    }
                                  >
                                    <div
                                      onClick={() => {
                                        if (!canEdit || fromRole) return;
                                        if (isSelected) {
                                          setFormExtraPermissions(
                                            formExtraPermissions.filter((p) => p !== perm.permission_code)
                                          );
                                        } else {
                                          setFormExtraPermissions([...formExtraPermissions, perm.permission_code]);
                                        }
                                      }}
                                      className="flex items-center gap-2 p-2 rounded-lg transition-colors"
                                      style={{
                                        background: isSelected
                                          ? darkMode
                                            ? "#06b6d420"
                                            : "#06b6d410"
                                          : "transparent",
                                        opacity: fromRole ? 0.5 : 1,
                                        cursor: canEdit && !fromRole ? "pointer" : "default",
                                      }}
                                    >
                                      <Checkbox
                                        checked={isSelected || fromRole}
                                        disabled={!canEdit || fromRole}
                                      />
                                      <div className="flex-1 min-w-0">
                                        <p className="text-sm font-medium truncate">
                                          {perm.name || perm.permission_code}
                                        </p>
                                      </div>
                                      {fromRole && (
                                        <Badge variant="outline" className="text-xs shrink-0">
                                          {t("users.edit.extraPermissions.roleBadge")}
                                        </Badge>
                                      )}
                                    </div>
                                  </Tooltip>
                                );
                              })}
                            </div>
                          </motion.div>
                        )}
                      </AnimatePresence>
                    </div>
                  );
                })}
              </div>
            ) : (
              <p className="text-sm text-muted-foreground">{t("users.edit.extraPermissions.noAccess")}</p>
            )}
          </motion.div>
        </div>
      </div>

      {/* Fixed bottom save bar */}
      <AnimatePresence>
        {hasChanges && (
          <motion.div
            initial={{ y: 100, opacity: 0 }}
            animate={{ y: 0, opacity: 1 }}
            exit={{ y: 100, opacity: 0 }}
            className="fixed bottom-0 left-0 right-0 z-50"
            style={{
              marginLeft: "inherit",
            }}
          >
            <div
              className="mx-auto max-w-4xl px-6 py-4 rounded-t-2xl flex items-center justify-between gap-4"
              style={{
                background: darkMode
                  ? `rgba(${parseInt(colors.neutral[900].slice(1, 3), 16)}, ${parseInt(colors.neutral[900].slice(3, 5), 16)}, ${parseInt(colors.neutral[900].slice(5, 7), 16)}, 0.95)`
                  : "rgba(255, 255, 255, 0.95)",
                backdropFilter: "blur(12px)",
                boxShadow: "0 -4px 24px rgba(0,0,0,0.1)",
                border: `1px solid ${darkMode ? colors.neutral[700] : colors.neutral[200]}`,
                borderBottom: "none",
              }}
            >
              <div className="flex items-center gap-2 text-sm">
                <ExclamationCircleOutlined style={{ color: "#f97316" }} />
                <span>{t("users.edit.unsavedChanges")}</span>
              </div>
              <div className="flex items-center gap-3">
                <Button
                  variant="outline"
                  onClick={() => {
                    if (user) {
                      setFormDisplayName(user.display_name || "");
                      setFormDepartment(user.department || "");
                      setFormStatus(user.status);
                      setFormRoles(user.roles);
                      setFormExtraPermissions(user.extra_permissions || []);
                    }
                  }}
                >
                  {t("common.reset")}
                </Button>
                <Button
                  onClick={handleSave}
                  disabled={isSaving}
                  className="min-w-[100px]"
                  style={{
                    background: `linear-gradient(135deg, ${colors.primary[500]}, #06b6d4)`,
                  }}
                >
                  {isSaving ? (
                    <>
                      <LoadingOutlined className="mr-2" />
                      {t("common.saving")}
                    </>
                  ) : (
                    <>
                      <CheckOutlined className="mr-2" />
                      {t("users.edit.saveChanges")}
                    </>
                  )}
                </Button>
              </div>
            </div>
          </motion.div>
        )}
      </AnimatePresence>

      <style>{`
        .user-edit-page {
          font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
        }

        .role-card:hover {
          transform: translateY(-1px);
        }

        .permission-category:hover {
          border-color: ${colors.primary[500]}50 !important;
        }

        .line-clamp-2 {
          display: -webkit-box;
          -webkit-line-clamp: 2;
          -webkit-box-orient: vertical;
          overflow: hidden;
        }
      `}</style>
    </div>
  );
}
