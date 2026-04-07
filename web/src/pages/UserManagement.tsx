import { useState, useEffect, useMemo, useCallback } from "react";
import { useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { ArrowUpDown, ArrowUp, ArrowDown, ChevronsLeft, ChevronsRight, Trash2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Badge } from "@/components/ui/badge";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
// AlertDialog removed — was causing page refresh on action click
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Checkbox } from "@/components/ui/checkbox";
import { useAuthStore } from "@/store/useAuthStore";
import {
  listUsers,
  createUser,
  updateUser,
  deleteUser,
  resetUserPassword,
  enableUser,
  disableUser,
  listRoles,
  listPermissions,
} from "@/api/users";
import type { RoleResponse, UserResponse, PermissionResponse } from "@/api/users";

export function UserManagementPage() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { hasPermission, user: currentUser } = useAuthStore();

  // Permissions
  const canCreate = hasPermission("user:create");
  const canEdit = hasPermission("user:edit");
  const canDelete = hasPermission("user:delete");
  const canViewRoles = hasPermission("role:list");

  // State
  const [users, setUsers] = useState<UserResponse[]>([]);
  const [roles, setRoles] = useState<RoleResponse[]>([]);
  const [permissions, setPermissions] = useState<PermissionResponse[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);
  const [search, setSearch] = useState("");
  const [statusFilter, setStatusFilter] = useState<string>("all");
  const [isLoading, setIsLoading] = useState(false);

  // Sorting state
  type SortField = "email" | "display_name" | "status" | "last_login_at";
  type SortDirection = "asc" | "desc";
  const [sortField, setSortField] = useState<SortField | null>(null);
  const [sortDirection, setSortDirection] = useState<SortDirection>("asc");

  // Page size options
  const pageSizeOptions = [10, 20, 50];

  // Modal states
  const [showCreateModal, setShowCreateModal] = useState(false);
  const [showEditModal, setShowEditModal] = useState(false);
  const [selectedUser, setSelectedUser] = useState<UserResponse | null>(null);
  const [showDeleteDialog, setShowDeleteDialog] = useState(false);
  const [showResetPasswordDialog, setShowResetPasswordDialog] = useState(false);
  const [selectedUserIds, setSelectedUserIds] = useState<Set<string>>(new Set());
  const [isDeleting, setIsDeleting] = useState(false);
  const [showBatchDeleteDialog, setShowBatchDeleteDialog] = useState(false);

  // Form states
  const [formEmail, setFormEmail] = useState("");
  const [formDisplayName, setFormDisplayName] = useState("");
  const [formDepartment, setFormDepartment] = useState<string>("");
  const [formRoles, setFormRoles] = useState<string[]>(["user"]);
  const [formExtraPermissions, setFormExtraPermissions] = useState<string[]>([]);
  const [formStatus, setFormStatus] = useState("active");
  const [formError, setFormError] = useState("");

  // Department options
  const departmentOptions = [
    { value: "cs", label: t("user.departments.cs") },
    { value: "sales", label: t("user.departments.sales") },
    { value: "tech", label: t("user.departments.tech") },
    { value: "admin", label: t("user.departments.admin") },
  ];

  // Load users
  const loadUsers = async () => {
    setIsLoading(true);
    try {
      const response = await listUsers({
        page,
        page_size: pageSize,
        search: search || undefined,
        status: statusFilter === "all" ? undefined : statusFilter,
      });
      setUsers(response.users);
      setTotal(response.total);
    } catch (err) {
      console.error("Failed to load users:", err);
    } finally {
      setIsLoading(false);
    }
  };

  // Load roles
  const loadRoles = async () => {
    if (!canViewRoles) {
      setRoles([]);
      return;
    }
    try {
      const response = await listRoles();
      setRoles(response.roles);
    } catch (err) {
      console.error("Failed to load roles:", err);
    }
  };

  // Load permissions
  const loadPermissions = async () => {
    if (!canViewRoles) {
      setPermissions([]);
      return;
    }
    try {
      const response = await listPermissions();
      setPermissions(response.permissions);
    } catch (err) {
      console.error("Failed to load permissions:", err);
    }
  };

  useEffect(() => {
    loadUsers();
  }, [page, pageSize, statusFilter]);

  // Reset to page 1 when page size changes
  useEffect(() => {
    setPage(1);
  }, [pageSize]);

  // Handle sorting
  const handleSort = (field: SortField) => {
    if (sortField === field) {
      setSortDirection(sortDirection === "asc" ? "desc" : "asc");
    } else {
      setSortField(field);
      setSortDirection("asc");
    }
  };

  // Sort users client-side
  const sortedUsers = useMemo(() => {
    if (!sortField) return users;

    return [...users].sort((a, b) => {
      let aVal: string | number | null = a[sortField];
      let bVal: string | number | null = b[sortField];

      // Handle null/undefined values
      if (aVal == null) aVal = "";
      if (bVal == null) bVal = "";

      // String comparison
      if (typeof aVal === "string" && typeof bVal === "string") {
        const result = aVal.localeCompare(bVal);
        return sortDirection === "asc" ? result : -result;
      }

      return 0;
    });
  }, [users, sortField, sortDirection]);

  // Sortable header component
  const SortableHeader = ({ field, children }: { field: SortField; children: React.ReactNode }) => (
    <button
      className="flex items-center gap-1 hover:text-foreground transition-colors"
      onClick={() => handleSort(field)}
    >
      {children}
      {sortField === field ? (
        sortDirection === "asc" ? (
          <ArrowUp className="h-4 w-4" />
        ) : (
          <ArrowDown className="h-4 w-4" />
        )
      ) : (
        <ArrowUpDown className="h-4 w-4 opacity-50" />
      )}
    </button>
  );

  useEffect(() => {
    loadRoles();
    loadPermissions();
  }, [canViewRoles]);

  useEffect(() => {
    const timer = setTimeout(() => {
      if (page === 1) {
        loadUsers();
      } else {
        setPage(1);
      }
    }, 300);
    return () => clearTimeout(timer);
  }, [search]);

  // Create user
  const handleCreate = async () => {
    setFormError("");
    try {
      await createUser({
        email: formEmail,
        display_name: formDisplayName,
        department: formDepartment || undefined,
        roles: formRoles,
      });
      setShowCreateModal(false);
      resetForm();
      loadUsers();
    } catch (err: unknown) {
      const axiosError = err as { response?: { data?: { detail?: string } } };
      setFormError(axiosError.response?.data?.detail || "Failed to create user");
    }
  };

  // Update user
  const handleUpdate = async () => {
    if (!selectedUser) return;
    setFormError("");
    try {
      await updateUser(selectedUser.user_id, {
        display_name: formDisplayName,
        department: formDepartment || undefined,
        roles: formRoles,
        extra_permissions: formExtraPermissions,
        status: formStatus,
      });
      setShowEditModal(false);
      resetForm();
      loadUsers();
    } catch (err: unknown) {
      const axiosError = err as { response?: { data?: { detail?: string } } };
      setFormError(axiosError.response?.data?.detail || "Failed to update user");
    }
  };

  // Delete single user
  const handleDelete = useCallback(async () => {
    if (!selectedUser) return;
    setIsDeleting(true);
    try {
      await deleteUser(selectedUser.user_id);
    } catch (err) {
      console.error("Failed to delete user:", err);
    }
    setIsDeleting(false);
    setShowDeleteDialog(false);
    setSelectedUser(null);
    loadUsers();
  }, [selectedUser]);

  // Batch delete users
  const handleBatchDelete = useCallback(async () => {
    if (selectedUserIds.size === 0) return;
    setIsDeleting(true);
    const ids = Array.from(selectedUserIds);
    for (const id of ids) {
      try {
        await deleteUser(id);
      } catch (err) {
        console.error(`Failed to delete user ${id}:`, err);
      }
    }
    setIsDeleting(false);
    setShowBatchDeleteDialog(false);
    setSelectedUserIds(new Set());
    loadUsers();
  }, [selectedUserIds]);

  // Toggle selection for a single user
  const toggleUserSelection = (userId: string) => {
    setSelectedUserIds(prev => {
      const next = new Set(prev);
      if (next.has(userId)) next.delete(userId);
      else next.add(userId);
      return next;
    });
  };

  // Select/deselect all deletable users on current page
  const toggleSelectAll = () => {
    const deletableUsers = sortedUsers.filter(
      u => u.user_id !== currentUser?.user_id && u.user_id !== "admin"
    );
    if (selectedUserIds.size === deletableUsers.length) {
      setSelectedUserIds(new Set());
    } else {
      setSelectedUserIds(new Set(deletableUsers.map(u => u.user_id)));
    }
  };

  // Reset password
  const handleResetPassword = async () => {
    if (!selectedUser) return;
    try {
      await resetUserPassword(selectedUser.user_id);
      setShowResetPasswordDialog(false);
      setSelectedUser(null);
    } catch (err) {
      console.error("Failed to reset password:", err);
    }
  };

  // Toggle user status
  const handleToggleStatus = async (user: UserResponse) => {
    try {
      if (user.status === "active") {
        await disableUser(user.user_id);
      } else {
        await enableUser(user.user_id);
      }
      loadUsers();
    } catch (err) {
      console.error("Failed to toggle user status:", err);
    }
  };

  // Reset form
  const resetForm = () => {
    setFormEmail("");
    setFormDisplayName("");
    setFormDepartment("");
    setFormRoles(["user"]);
    setFormExtraPermissions([]);
    setFormStatus("active");
    setFormError("");
  };

  const totalPages = Math.ceil(total / pageSize);

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <div className="text-xl font-semibold">{t('users.title')}</div>
          {canDelete && selectedUserIds.size > 0 && (
            <Button
              variant="destructive"
              size="sm"
              onClick={() => setShowBatchDeleteDialog(true)}
            >
              <Trash2 className="h-4 w-4 mr-1.5" />
              {t('users.actions.batchDelete', `Delete (${selectedUserIds.size})`)}
            </Button>
          )}
        </div>
        {canCreate && (
          <Button onClick={() => setShowCreateModal(true)}>
            {t('users.addUser')}
          </Button>
        )}
      </div>

      {/* Filters */}
      <div className="flex gap-4">
        <Input
          placeholder={t('users.searchPlaceholder')}
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          className="max-w-sm"
        />
        <Select value={statusFilter} onValueChange={setStatusFilter}>
          <SelectTrigger className="w-[150px]">
            <SelectValue placeholder={t('users.allStatus')} />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">{t('users.allStatus')}</SelectItem>
            <SelectItem value="active">{t('common.active')}</SelectItem>
            <SelectItem value="disabled">{t('common.disabled')}</SelectItem>
          </SelectContent>
        </Select>
      </div>

      {/* Users table */}
      <div className="rounded-xl border border-border/60 bg-card/50 backdrop-blur-sm shadow-sm overflow-hidden">
        <Table>
          <TableHeader>
            <TableRow>
              {canDelete && (
                <TableHead className="w-10">
                  <Checkbox
                    checked={sortedUsers.length > 0 && selectedUserIds.size === sortedUsers.filter(u => u.user_id !== currentUser?.user_id && u.user_id !== "admin").length}
                    onCheckedChange={toggleSelectAll}
                    aria-label="Select all"
                  />
                </TableHead>
              )}
              <TableHead>
                <SortableHeader field="email">{t('users.fields.email')}</SortableHeader>
              </TableHead>
              <TableHead>
                <SortableHeader field="display_name">{t('users.fields.displayName')}</SortableHeader>
              </TableHead>
              <TableHead>{t('users.fields.department')}</TableHead>
              <TableHead>{t('users.fields.roles')}</TableHead>
              <TableHead>
                <SortableHeader field="status">{t('users.fields.status')}</SortableHeader>
              </TableHead>
              <TableHead>
                <SortableHeader field="last_login_at">{t('users.fields.lastLogin')}</SortableHeader>
              </TableHead>
              <TableHead className="text-right">{t('common.actions')}</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {isLoading ? (
              <TableRow>
                <TableCell colSpan={canDelete ? 8 : 7} className="text-center py-8">
                  {t('users.loading')}
                </TableCell>
              </TableRow>
            ) : sortedUsers.length === 0 ? (
              <TableRow>
                <TableCell colSpan={canDelete ? 8 : 7} className="text-center py-8">
                  {t('users.noUsers')}
                </TableCell>
              </TableRow>
            ) : (
              sortedUsers.map((user) => (
                <TableRow key={user.user_id} className="transition-colors hover:bg-accent">
                  {canDelete && (
                    <TableCell>
                      {user.user_id !== currentUser?.user_id && user.user_id !== "admin" ? (
                        <Checkbox
                          checked={selectedUserIds.has(user.user_id)}
                          onCheckedChange={() => toggleUserSelection(user.user_id)}
                          aria-label={`Select ${user.email}`}
                        />
                      ) : null}
                    </TableCell>
                  )}
                  <TableCell>{user.email}</TableCell>
                  <TableCell>{user.display_name}</TableCell>
                  <TableCell>
                    {user.department ? (
                      <Badge variant="outline" className="bg-amber-500/10 text-amber-700 dark:text-amber-400 border-amber-500/30">
                        {departmentOptions.find(d => d.value === user.department)?.label || user.department}
                      </Badge>
                    ) : (
                      <span className="text-muted-foreground text-sm">{t('common.notSet')}</span>
                    )}
                  </TableCell>
                  <TableCell>
                    <div className="flex gap-1.5 flex-wrap">
                      {user.roles.map((role) => {
                        // Role color differentiation
                        const roleColors: Record<string, string> = {
                          admin: "bg-indigo-500/10 text-indigo-600 dark:text-indigo-400 border-indigo-500/20",
                          manager: "bg-sky-500/10 text-sky-600 dark:text-sky-400 border-sky-500/20",
                          user: "bg-slate-500/10 text-slate-500 dark:text-slate-400 border-slate-400/20",
                        };
                        const colorClass = roleColors[role.toLowerCase()] || roleColors.user;
                        return (
                          <Badge
                            key={role}
                            variant="outline"
                            className={`font-medium ${colorClass}`}
                          >
                            {role}
                          </Badge>
                        );
                      })}
                    </div>
                  </TableCell>
                  <TableCell>
                    <Badge
                      variant="outline"
                      className={
                        user.status === "active"
                          ? "bg-emerald-500/15 text-emerald-700 dark:text-emerald-400 border-emerald-500/30"
                          : "bg-red-500/15 text-red-700 dark:text-red-400 border-red-500/30"
                      }
                    >
                      <span className="flex items-center gap-1.5">
                        <span
                          className={`h-1.5 w-1.5 rounded-full ${
                            user.status === "active"
                              ? "bg-emerald-500 status-pulse"
                              : "bg-red-500"
                          }`}
                        />
                        {user.status === "active" ? t('common.active') : t('common.disabled')}
                      </span>
                    </Badge>
                  </TableCell>
                  <TableCell>
                    {user.last_login_at
                      ? new Date(user.last_login_at).toLocaleString()
                      : t('common.never')}
                  </TableCell>
                  <TableCell className="text-right">
                    <div className="flex gap-2 justify-end">
                      {canEdit && (
                        <>
                          <Button
                            variant="outline"
                            size="sm"
                            onClick={() => navigate(`/users/${user.user_id}/edit`)}
                          >
                            {t('users.actions.edit')}
                          </Button>
                          <Button
                            variant="outline"
                            size="sm"
                            onClick={() => {
                              setSelectedUser(user);
                              setShowResetPasswordDialog(true);
                            }}
                          >
                            {t('users.actions.resetPassword')}
                          </Button>
                          {user.user_id !== currentUser?.user_id && user.user_id !== "admin" && (
                            <Button
                              variant="outline"
                              size="sm"
                              onClick={() => handleToggleStatus(user)}
                            >
                              {user.status === "active" ? t('users.actions.disable') : t('users.actions.enable')}
                            </Button>
                          )}
                        </>
                      )}
                      {canDelete && user.user_id !== currentUser?.user_id && user.user_id !== "admin" && (
                        <Button
                          variant="destructive"
                          size="sm"
                          onClick={() => {
                            setSelectedUser(user);
                            setShowDeleteDialog(true);
                          }}
                        >
                          {t('users.actions.delete')}
                        </Button>
                      )}
                    </div>
                  </TableCell>
                </TableRow>
              ))
            )}
          </TableBody>
        </Table>
      </div>

      {/* Pagination */}
      <div className="flex items-center justify-between flex-wrap gap-4">
        <div className="flex items-center gap-4">
          <div className="text-sm text-muted-foreground">
            {t('users.pagination.showing', {
              start: total > 0 ? (page - 1) * pageSize + 1 : 0,
              end: Math.min(page * pageSize, total),
              total: total
            })}
          </div>
          <div className="flex items-center gap-2">
            <span className="text-sm text-muted-foreground">{t('users.pagination.page', { current: page, total: Math.max(totalPages, 1) })}</span>
          </div>
        </div>
        <div className="flex items-center gap-4">
          {/* Page size selector */}
          <div className="flex items-center gap-2">
            <Select value={pageSize.toString()} onValueChange={(v) => setPageSize(Number(v))}>
              <SelectTrigger className="w-[100px]">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {pageSizeOptions.map((size) => (
                  <SelectItem key={size} value={size.toString()}>
                    {t('users.pagination.perPage', { count: size })}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          {/* Pagination buttons */}
          <div className="flex gap-1">
            <Button
              variant="outline"
              size="icon"
              className="h-8 w-8"
              onClick={() => setPage(1)}
              disabled={page <= 1}
              title={t('users.pagination.first')}
            >
              <ChevronsLeft className="h-4 w-4" />
            </Button>
            <Button
              variant="outline"
              size="sm"
              onClick={() => setPage(page - 1)}
              disabled={page <= 1}
            >
              {t('users.pagination.prev')}
            </Button>
            <Button
              variant="outline"
              size="sm"
              onClick={() => setPage(page + 1)}
              disabled={page >= totalPages}
            >
              {t('users.pagination.next')}
            </Button>
            <Button
              variant="outline"
              size="icon"
              className="h-8 w-8"
              onClick={() => setPage(totalPages)}
              disabled={page >= totalPages}
              title={t('users.pagination.last')}
            >
              <ChevronsRight className="h-4 w-4" />
            </Button>
          </div>
        </div>
      </div>

      {/* Create User Modal */}
      <Dialog open={showCreateModal} onOpenChange={setShowCreateModal}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Create User</DialogTitle>
            <DialogDescription>
              Create a new user account. Initial password will be 111111.
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-4">
            <div className="space-y-2">
              <Label>Email</Label>
              <Input
                type="email"
                placeholder="name@hejazfs.com.au"
                value={formEmail}
                onChange={(e) => setFormEmail(e.target.value)}
              />
            </div>
            <div className="space-y-2">
              <Label>Display Name</Label>
              <Input
                value={formDisplayName}
                onChange={(e) => setFormDisplayName(e.target.value)}
              />
            </div>
            <div className="space-y-2">
              <Label>Department</Label>
              <Select value={formDepartment} onValueChange={setFormDepartment}>
                <SelectTrigger>
                  <SelectValue placeholder="Select department" />
                </SelectTrigger>
                <SelectContent>
                  {departmentOptions.map((dept) => (
                    <SelectItem key={dept.value} value={dept.value}>
                      {dept.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              <p className="text-xs text-muted-foreground">
                Department determines user's access to knowledge base and agents
              </p>
            </div>
            <div className="space-y-2">
              <Label>Roles</Label>
              {canViewRoles ? (
                <div className="space-y-2">
                  {roles.map((role) => (
                    <div key={role.role_name} className="flex items-center gap-2">
                      <Checkbox
                        id={`role-${role.role_name}`}
                        checked={formRoles.includes(role.role_name)}
                        onCheckedChange={(checked) => {
                          if (checked) {
                            setFormRoles([...formRoles, role.role_name]);
                          } else {
                            setFormRoles(formRoles.filter((r) => r !== role.role_name));
                          }
                        }}
                      />
                      <label htmlFor={`role-${role.role_name}`} className="text-sm">
                        {role.role_name} - {role.description}
                      </label>
                    </div>
                  ))}
                </div>
              ) : (
                <div className="text-sm text-muted-foreground">
                  You do not have permission to view role list. New users will be
                  assigned the default role.
                </div>
              )}
            </div>
            {formError && <div className="text-sm text-red-500">{formError}</div>}
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setShowCreateModal(false)}>
              Cancel
            </Button>
            <Button onClick={handleCreate}>Create</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Edit User Modal */}
      <Dialog open={showEditModal} onOpenChange={setShowEditModal}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Edit User</DialogTitle>
            <DialogDescription>
              Update user information for {selectedUser?.email}
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-4">
            <div className="space-y-2">
              <Label>Display Name</Label>
              <Input
                value={formDisplayName}
                onChange={(e) => setFormDisplayName(e.target.value)}
              />
            </div>
            <div className="space-y-2">
              <Label>Department</Label>
              <Select value={formDepartment} onValueChange={setFormDepartment}>
                <SelectTrigger>
                  <SelectValue placeholder="Select department" />
                </SelectTrigger>
                <SelectContent>
                  {departmentOptions.map((dept) => (
                    <SelectItem key={dept.value} value={dept.value}>
                      {dept.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              <p className="text-xs text-muted-foreground">
                Department determines user's access to knowledge base and agents
              </p>
            </div>
            <div className="space-y-2">
              <Label>Status</Label>
              <Select value={formStatus} onValueChange={setFormStatus}>
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="active">Active</SelectItem>
                  <SelectItem value="disabled">Disabled</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-2">
              <Label>Roles</Label>
              {canViewRoles ? (
                <div className="space-y-2">
                  {roles.map((role) => (
                    <div key={role.role_name} className="flex items-center gap-2">
                      <Checkbox
                        id={`edit-role-${role.role_name}`}
                        checked={formRoles.includes(role.role_name)}
                        onCheckedChange={(checked) => {
                          if (checked) {
                            setFormRoles([...formRoles, role.role_name]);
                          } else {
                            setFormRoles(formRoles.filter((r) => r !== role.role_name));
                          }
                        }}
                      />
                      <label htmlFor={`edit-role-${role.role_name}`} className="text-sm">
                        {role.role_name} - {role.description}
                      </label>
                    </div>
                  ))}
                </div>
              ) : (
                <div className="text-sm text-muted-foreground">
                  You do not have permission to view role list.
                </div>
              )}
            </div>
            <div className="space-y-2">
              <Label>Extra Permissions (Direct Assignment)</Label>
              <p className="text-xs text-muted-foreground mb-2">
                These permissions are assigned directly to the user, in addition to role-based permissions.
              </p>
              {canViewRoles ? (
                <div className="max-h-48 overflow-y-auto border rounded-md p-3 space-y-3">
                  {/* Group permissions by category */}
                  {Object.entries(
                    permissions.reduce((acc, perm) => {
                      const cat = perm.category || "Other";
                      if (!acc[cat]) acc[cat] = [];
                      acc[cat].push(perm);
                      return acc;
                    }, {} as Record<string, PermissionResponse[]>)
                  ).map(([category, perms]) => (
                    <div key={category} className="space-y-1">
                      <div className="text-xs font-medium text-muted-foreground uppercase tracking-wide">
                        {category}
                      </div>
                      {perms.map((perm) => (
                        <div key={perm.permission_code} className="flex items-center gap-2 ml-2">
                          <Checkbox
                            id={`extra-perm-${perm.permission_code}`}
                            checked={formExtraPermissions.includes(perm.permission_code)}
                            onCheckedChange={(checked) => {
                              if (checked) {
                                setFormExtraPermissions([...formExtraPermissions, perm.permission_code]);
                              } else {
                                setFormExtraPermissions(
                                  formExtraPermissions.filter((p) => p !== perm.permission_code)
                                );
                              }
                            }}
                          />
                          <label
                            htmlFor={`extra-perm-${perm.permission_code}`}
                            className="text-sm cursor-pointer"
                            title={perm.description || perm.permission_code}
                          >
                            {perm.name || perm.permission_code}
                          </label>
                        </div>
                      ))}
                    </div>
                  ))}
                </div>
              ) : (
                <div className="text-sm text-muted-foreground">
                  You do not have permission to view permissions list.
                </div>
              )}
            </div>
            {formError && <div className="text-sm text-red-500">{formError}</div>}
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setShowEditModal(false)}>
              Cancel
            </Button>
            <Button onClick={handleUpdate}>Save</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Delete Confirmation — uses Dialog instead of AlertDialog to prevent page refresh */}
      <Dialog open={showDeleteDialog} onOpenChange={setShowDeleteDialog}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Delete User</DialogTitle>
            <DialogDescription>
              Are you sure you want to delete {selectedUser?.email}? This action cannot be undone.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setShowDeleteDialog(false)} disabled={isDeleting}>Cancel</Button>
            <Button variant="destructive" onClick={handleDelete} disabled={isDeleting}>
              {isDeleting ? "Deleting..." : "Delete"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Batch Delete Confirmation */}
      <Dialog open={showBatchDeleteDialog} onOpenChange={setShowBatchDeleteDialog}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Delete {selectedUserIds.size} Users</DialogTitle>
            <DialogDescription>
              Are you sure you want to delete {selectedUserIds.size} selected users? This action cannot be undone.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setShowBatchDeleteDialog(false)} disabled={isDeleting}>Cancel</Button>
            <Button variant="destructive" onClick={handleBatchDelete} disabled={isDeleting}>
              {isDeleting ? "Deleting..." : `Delete ${selectedUserIds.size} Users`}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Reset Password Confirmation */}
      <Dialog open={showResetPasswordDialog} onOpenChange={setShowResetPasswordDialog}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Reset Password</DialogTitle>
            <DialogDescription>
              Reset password for {selectedUser?.email} to default (111111)? The user will be required to change password on next login.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setShowResetPasswordDialog(false)}>Cancel</Button>
            <Button onClick={handleResetPassword}>Reset Password</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
