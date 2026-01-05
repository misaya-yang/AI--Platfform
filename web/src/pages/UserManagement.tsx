import { useState, useEffect } from "react";
import { useNavigate } from "react-router-dom";
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
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
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
  const [pageSize] = useState(20);
  const [search, setSearch] = useState("");
  const [statusFilter, setStatusFilter] = useState<string>("all");
  const [isLoading, setIsLoading] = useState(false);

  // Modal states
  const [showCreateModal, setShowCreateModal] = useState(false);
  const [showEditModal, setShowEditModal] = useState(false);
  const [selectedUser, setSelectedUser] = useState<UserResponse | null>(null);
  const [showDeleteDialog, setShowDeleteDialog] = useState(false);
  const [showResetPasswordDialog, setShowResetPasswordDialog] = useState(false);

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
    { value: "cs", label: "客服部 (CS)" },
    { value: "sales", label: "销售部 (Sales)" },
    { value: "tech", label: "技术部 (Tech)" },
    { value: "admin", label: "管理部 (Admin)" },
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
  }, [page, statusFilter]);

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

  // Delete user
  const handleDelete = async () => {
    if (!selectedUser) return;
    try {
      await deleteUser(selectedUser.user_id);
      setShowDeleteDialog(false);
      setSelectedUser(null);
      loadUsers();
    } catch (err) {
      console.error("Failed to delete user:", err);
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

  // Open edit modal
  const openEditModal = (user: UserResponse) => {
    setSelectedUser(user);
    setFormDisplayName(user.display_name || "");
    setFormDepartment(user.department || "");
    setFormRoles(user.roles);
    setFormExtraPermissions(user.extra_permissions || []);
    setFormStatus(user.status);
    setShowEditModal(true);
  };

  const totalPages = Math.ceil(total / pageSize);

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div className="text-xl font-semibold">User Management</div>
        {canCreate && (
          <Button onClick={() => setShowCreateModal(true)}>Add User</Button>
        )}
      </div>

      {/* Filters */}
      <div className="flex gap-4">
        <Input
          placeholder="Search by email or name..."
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          className="max-w-sm"
        />
        <Select value={statusFilter} onValueChange={setStatusFilter}>
          <SelectTrigger className="w-[150px]">
            <SelectValue placeholder="All Status" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">All Status</SelectItem>
            <SelectItem value="active">Active</SelectItem>
            <SelectItem value="disabled">Disabled</SelectItem>
          </SelectContent>
        </Select>
      </div>

      {/* Users table */}
      <div className="rounded-md border">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Email</TableHead>
              <TableHead>Display Name</TableHead>
              <TableHead>Department</TableHead>
              <TableHead>Roles</TableHead>
              <TableHead>Status</TableHead>
              <TableHead>Last Login</TableHead>
              <TableHead className="text-right">Actions</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {isLoading ? (
              <TableRow>
                <TableCell colSpan={7} className="text-center py-8">
                  Loading...
                </TableCell>
              </TableRow>
            ) : users.length === 0 ? (
              <TableRow>
                <TableCell colSpan={7} className="text-center py-8">
                  No users found
                </TableCell>
              </TableRow>
            ) : (
              users.map((user) => (
                <TableRow key={user.user_id}>
                  <TableCell>{user.email}</TableCell>
                  <TableCell>{user.display_name}</TableCell>
                  <TableCell>
                    {user.department ? (
                      <Badge variant="outline">
                        {departmentOptions.find(d => d.value === user.department)?.label || user.department}
                      </Badge>
                    ) : (
                      <span className="text-muted-foreground text-sm">-</span>
                    )}
                  </TableCell>
                  <TableCell>
                    <div className="flex gap-1 flex-wrap">
                      {user.roles.map((role) => (
                        <Badge key={role} variant="secondary">
                          {role}
                        </Badge>
                      ))}
                    </div>
                  </TableCell>
                  <TableCell>
                    <Badge
                      variant={user.status === "active" ? "default" : "destructive"}
                    >
                      {user.status}
                    </Badge>
                  </TableCell>
                  <TableCell>
                    {user.last_login_at
                      ? new Date(user.last_login_at).toLocaleString()
                      : "Never"}
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
                            Edit
                          </Button>
                          <Button
                            variant="outline"
                            size="sm"
                            onClick={() => {
                              setSelectedUser(user);
                              setShowResetPasswordDialog(true);
                            }}
                          >
                            Reset PW
                          </Button>
                          {user.user_id !== currentUser?.user_id && user.user_id !== "admin" && (
                            <Button
                              variant="outline"
                              size="sm"
                              onClick={() => handleToggleStatus(user)}
                            >
                              {user.status === "active" ? "Disable" : "Enable"}
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
                          Delete
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
      {totalPages > 1 && (
        <div className="flex items-center justify-between">
          <div className="text-sm text-muted-foreground">
            Showing {(page - 1) * pageSize + 1} to{" "}
            {Math.min(page * pageSize, total)} of {total} users
          </div>
          <div className="flex gap-2">
            <Button
              variant="outline"
              size="sm"
              onClick={() => setPage(page - 1)}
              disabled={page <= 1}
            >
              Previous
            </Button>
            <Button
              variant="outline"
              size="sm"
              onClick={() => setPage(page + 1)}
              disabled={page >= totalPages}
            >
              Next
            </Button>
          </div>
        </div>
      )}

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

      {/* Delete Confirmation */}
      <AlertDialog open={showDeleteDialog} onOpenChange={setShowDeleteDialog}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Delete User</AlertDialogTitle>
            <AlertDialogDescription>
              Are you sure you want to delete {selectedUser?.email}? This action
              cannot be undone.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction onClick={handleDelete}>Delete</AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* Reset Password Confirmation */}
      <AlertDialog
        open={showResetPasswordDialog}
        onOpenChange={setShowResetPasswordDialog}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Reset Password</AlertDialogTitle>
            <AlertDialogDescription>
              Reset password for {selectedUser?.email} to default (111111)? The
              user will be required to change password on next login.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction onClick={handleResetPassword}>
              Reset Password
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}
