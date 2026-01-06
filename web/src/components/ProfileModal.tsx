import { useState, useEffect } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { useAuthStore } from "@/store/useAuthStore";
import { api } from "@/lib/api";
import { Badge } from "@/components/ui/badge";

interface ProfileModalProps {
  open: boolean;
  onClose: () => void;
}

// Department display mapping
const departmentLabels: Record<string, string> = {
  cs: "客服部 (CS)",
  sales: "销售部 (Sales)",
  tech: "技术部 (Tech)",
  admin: "管理部 (Admin)",
};

export function ProfileModal({ open, onClose }: ProfileModalProps) {
  const { user, updateUser } = useAuthStore();

  const [displayName, setDisplayName] = useState("");
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState("");
  const [success, setSuccess] = useState("");

  // Initialize form when modal opens
  useEffect(() => {
    if (open && user) {
      setDisplayName(user.display_name || "");
      setError("");
      setSuccess("");
    }
  }, [open, user]);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError("");
    setSuccess("");
    setIsLoading(true);

    try {
      // Update profile via API
      await api.put(`/api/v1/users/${user?.user_id}/profile`, {
        display_name: displayName.trim(),
      });

      // Update local state
      updateUser({ display_name: displayName.trim() });
      setSuccess("个人信息已更新");

      // Close after short delay
      setTimeout(() => {
        onClose();
      }, 1000);
    } catch (err: unknown) {
      const axiosError = err as { response?: { data?: { detail?: string } } };
      setError(axiosError.response?.data?.detail || "更新失败，请重试");
    } finally {
      setIsLoading(false);
    }
  };

  if (!user) return null;

  return (
    <Dialog open={open} onOpenChange={(nextOpen) => !nextOpen && onClose()}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>个人设置</DialogTitle>
          <DialogDescription>
            查看和修改您的个人信息
          </DialogDescription>
        </DialogHeader>

        <form onSubmit={handleSubmit} className="space-y-4">
          {/* Email - Read only */}
          <div className="space-y-2">
            <Label className="text-muted-foreground">邮箱</Label>
            <Input
              value={user.email || ""}
              disabled
              className="bg-muted"
            />
            <p className="text-xs text-muted-foreground">邮箱无法修改</p>
          </div>

          {/* Display Name - Editable */}
          <div className="space-y-2">
            <Label htmlFor="displayName">显示名称</Label>
            <Input
              id="displayName"
              value={displayName}
              onChange={(e) => setDisplayName(e.target.value)}
              placeholder="请输入您的姓名"
              disabled={isLoading}
            />
          </div>

          {/* Department - Read only, assigned by admin */}
          <div className="space-y-2">
            <Label className="text-muted-foreground">所属部门</Label>
            <div className="flex items-center gap-2 p-2 bg-muted rounded-md min-h-[40px]">
              {user.department ? (
                <Badge variant="secondary">
                  {departmentLabels[user.department] || user.department}
                </Badge>
              ) : (
                <span className="text-sm text-muted-foreground">未分配部门</span>
              )}
            </div>
            <p className="text-xs text-muted-foreground">
              部门由管理员分配，如需修改请联系管理员
            </p>
          </div>

          {/* Roles - Read only */}
          <div className="space-y-2">
            <Label className="text-muted-foreground">角色</Label>
            <div className="flex flex-wrap gap-2 p-2 bg-muted rounded-md min-h-[40px]">
              {user.roles.map((role) => (
                <Badge key={role} variant="outline">
                  {role}
                </Badge>
              ))}
            </div>
          </div>

          {error && (
            <div className="text-sm text-red-500">{error}</div>
          )}
          {success && (
            <div className="text-sm text-green-500">{success}</div>
          )}

          <DialogFooter>
            <Button type="button" variant="outline" onClick={onClose}>
              取消
            </Button>
            <Button type="submit" disabled={isLoading}>
              {isLoading ? "保存中..." : "保存"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
