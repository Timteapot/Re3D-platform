$lsaTypeName = "Re3D.Platform.Native.LsaBatchLogonRightV1"
$lsaType = $lsaTypeName -as [type]

if ($null -eq $lsaType) {
    $lsaSource = @'
using System;
using System.ComponentModel;
using System.IO;
using System.Runtime.InteropServices;
using System.Security.Principal;
using Microsoft.Win32.SafeHandles;

namespace Re3D.Platform.Native
{
    public static class LsaBatchLogonRightV1
    {
        private const string RightName = "SeBatchLogonRight";
        private const uint PolicyCreateAccount = 0x00000010;
        private const uint PolicyLookupNames = 0x00000800;
        private const int StatusSuccess = 0;
        private const int StatusObjectNameNotFound = unchecked((int)0xC0000034);
        private const uint ErrorFileNotFound = 2;
        private const uint MaximumEnumeratedRights = 4096;

        [StructLayout(LayoutKind.Sequential)]
        private struct LsaUnicodeString
        {
            public ushort Length;
            public ushort MaximumLength;
            public IntPtr Buffer;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct LsaObjectAttributes
        {
            public uint Length;
            public IntPtr RootDirectory;
            public IntPtr ObjectName;
            public uint Attributes;
            public IntPtr SecurityDescriptor;
            public IntPtr SecurityQualityOfService;
        }

        private sealed class SafeLsaPolicyHandle :
            SafeHandleZeroOrMinusOneIsInvalid
        {
            public SafeLsaPolicyHandle() : base(true) { }

            protected override bool ReleaseHandle()
            {
                return LsaClose(handle) == StatusSuccess;
            }
        }

        private sealed class SafeLsaMemoryHandle :
            SafeHandleZeroOrMinusOneIsInvalid
        {
            public SafeLsaMemoryHandle() : base(true) { }

            protected override bool ReleaseHandle()
            {
                return LsaFreeMemory(handle) == StatusSuccess;
            }
        }

        private sealed class PinnedSid : IDisposable
        {
            private byte[] bytes;
            private GCHandle pin;
            private bool allocated;

            public PinnedSid(SecurityIdentifier sid)
            {
                if (sid == null) throw new ArgumentNullException("sid");
                bytes = new byte[sid.BinaryLength];
                sid.GetBinaryForm(bytes, 0);
                pin = GCHandle.Alloc(bytes, GCHandleType.Pinned);
                allocated = true;
            }

            ~PinnedSid() { Dispose(false); }

            public IntPtr Pointer
            {
                get
                {
                    if (!allocated)
                        throw new ObjectDisposedException("PinnedSid");
                    return pin.AddrOfPinnedObject();
                }
            }

            public void Dispose()
            {
                Dispose(true);
                GC.SuppressFinalize(this);
            }

            private void Dispose(bool disposing)
            {
                if (allocated)
                {
                    pin.Free();
                    allocated = false;
                }
                if (bytes != null)
                {
                    Array.Clear(bytes, 0, bytes.Length);
                    bytes = null;
                }
            }
        }

        private sealed class NativeUnicodeString : IDisposable
        {
            private LsaUnicodeString value;
            private bool allocated;

            public NativeUnicodeString(string text)
            {
                if (text == null) throw new ArgumentNullException("text");
                if (text.Length > 0x7ffe)
                    throw new ArgumentOutOfRangeException("text");
                value.Length = checked((ushort)(text.Length * 2));
                value.MaximumLength = checked((ushort)((text.Length + 1) * 2));
                value.Buffer = Marshal.StringToHGlobalUni(text);
                allocated = true;
            }

            ~NativeUnicodeString() { Dispose(false); }

            public LsaUnicodeString Value
            {
                get
                {
                    if (!allocated)
                        throw new ObjectDisposedException("NativeUnicodeString");
                    return value;
                }
            }

            public void Dispose()
            {
                Dispose(true);
                GC.SuppressFinalize(this);
            }

            private void Dispose(bool disposing)
            {
                if (allocated)
                {
                    Marshal.FreeHGlobal(value.Buffer);
                    value.Buffer = IntPtr.Zero;
                    value.Length = 0;
                    value.MaximumLength = 0;
                    allocated = false;
                }
            }
        }

        [DllImport("advapi32.dll", ExactSpelling = true, SetLastError = false)]
        private static extern int LsaOpenPolicy(
            IntPtr systemName,
            ref LsaObjectAttributes objectAttributes,
            uint desiredAccess,
            out SafeLsaPolicyHandle policyHandle
        );

        [DllImport("advapi32.dll", ExactSpelling = true, SetLastError = false)]
        private static extern int LsaEnumerateAccountRights(
            SafeLsaPolicyHandle policyHandle,
            IntPtr accountSid,
            out SafeLsaMemoryHandle userRights,
            out uint countOfRights
        );

        [DllImport("advapi32.dll", ExactSpelling = true, SetLastError = false)]
        private static extern int LsaAddAccountRights(
            SafeLsaPolicyHandle policyHandle,
            IntPtr accountSid,
            ref LsaUnicodeString userRights,
            uint countOfRights
        );

        [DllImport("advapi32.dll", ExactSpelling = true, SetLastError = false)]
        private static extern int LsaRemoveAccountRights(
            SafeLsaPolicyHandle policyHandle,
            IntPtr accountSid,
            byte allRights,
            ref LsaUnicodeString userRights,
            uint countOfRights
        );

        [DllImport("advapi32.dll", ExactSpelling = true, SetLastError = false)]
        private static extern int LsaClose(IntPtr objectHandle);

        [DllImport("advapi32.dll", ExactSpelling = true, SetLastError = false)]
        private static extern int LsaFreeMemory(IntPtr buffer);

        [DllImport("advapi32.dll", ExactSpelling = true, SetLastError = false)]
        private static extern uint LsaNtStatusToWinError(int status);

        static LsaBatchLogonRightV1()
        {
            int expectedUnicodeStringSize = IntPtr.Size == 8 ? 16 : 8;
            int expectedObjectAttributesSize = IntPtr.Size == 8 ? 48 : 24;
            if (
                Marshal.SizeOf(typeof(LsaUnicodeString)) != expectedUnicodeStringSize ||
                Marshal.SizeOf(typeof(LsaObjectAttributes)) != expectedObjectAttributesSize
            )
            {
                throw new PlatformNotSupportedException(
                    "Unexpected LSA native structure layout."
                );
            }
        }

        public static bool Has(string accountName)
        {
            return Has(ResolveAccount(accountName));
        }

        public static bool Add(string accountName)
        {
            SecurityIdentifier sid = ResolveAccount(accountName);
            if (Has(sid)) return false;

            using (
                SafeLsaPolicyHandle policy = OpenPolicy(
                    PolicyLookupNames | PolicyCreateAccount
                )
            )
            using (PinnedSid nativeSid = new PinnedSid(sid))
            using (
                NativeUnicodeString nativeRight =
                    new NativeUnicodeString(RightName)
            )
            {
                LsaUnicodeString right = nativeRight.Value;
                int status = LsaAddAccountRights(
                    policy,
                    nativeSid.Pointer,
                    ref right,
                    1
                );
                ThrowIfFailed(status, "LsaAddAccountRights");
            }

            if (!Has(sid))
                throw new InvalidOperationException(
                    "LSA reported success, but SeBatchLogonRight is not present."
                );
            return true;
        }

        public static bool Remove(string accountName)
        {
            SecurityIdentifier sid = ResolveAccount(accountName);
            if (!Has(sid)) return false;

            using (
                SafeLsaPolicyHandle policy = OpenPolicy(PolicyLookupNames)
            )
            using (PinnedSid nativeSid = new PinnedSid(sid))
            using (
                NativeUnicodeString nativeRight =
                    new NativeUnicodeString(RightName)
            )
            {
                LsaUnicodeString right = nativeRight.Value;
                int status = LsaRemoveAccountRights(
                    policy,
                    nativeSid.Pointer,
                    0,
                    ref right,
                    1
                );
                if (!IsMissingAccount(status))
                    ThrowIfFailed(status, "LsaRemoveAccountRights");
            }

            if (Has(sid))
                throw new InvalidOperationException(
                    "LSA reported success, but SeBatchLogonRight is still present."
                );
            return true;
        }

        private static bool Has(SecurityIdentifier sid)
        {
            using (
                SafeLsaPolicyHandle policy = OpenPolicy(PolicyLookupNames)
            )
            using (PinnedSid nativeSid = new PinnedSid(sid))
            {
                SafeLsaMemoryHandle rights;
                uint count;
                int status = LsaEnumerateAccountRights(
                    policy,
                    nativeSid.Pointer,
                    out rights,
                    out count
                );

                using (rights)
                {
                    if (IsMissingAccount(status)) return false;
                    ThrowIfFailed(status, "LsaEnumerateAccountRights");
                    if (count > MaximumEnumeratedRights)
                        throw new InvalidDataException(
                            "LSA returned an unreasonable account-right count."
                        );
                    if (count == 0) return false;
                    if (rights == null || rights.IsInvalid)
                        throw new InvalidDataException(
                            "LSA returned a null account-right buffer."
                        );

                    int elementSize = Marshal.SizeOf(typeof(LsaUnicodeString));
                    IntPtr baseAddress = rights.DangerousGetHandle();
                    for (uint index = 0; index < count; index++)
                    {
                        int offset = checked((int)(index * (uint)elementSize));
                        LsaUnicodeString item = (LsaUnicodeString)
                            Marshal.PtrToStructure(
                                IntPtr.Add(baseAddress, offset),
                                typeof(LsaUnicodeString)
                            );
                        ValidateReturnedString(item);
                        string name = item.Length == 0
                            ? String.Empty
                            : Marshal.PtrToStringUni(
                                item.Buffer,
                                item.Length / 2
                            );
                        if (
                            String.Equals(
                                name,
                                RightName,
                                StringComparison.OrdinalIgnoreCase
                            )
                        )
                            return true;
                    }
                    return false;
                }
            }
        }

        private static SecurityIdentifier ResolveAccount(string accountName)
        {
            if (String.IsNullOrWhiteSpace(accountName))
                throw new ArgumentException(
                    "Account name is required.",
                    "accountName"
                );
            IdentityReference translated = new NTAccount(accountName).Translate(
                typeof(SecurityIdentifier)
            );
            return (SecurityIdentifier)translated;
        }

        private static SafeLsaPolicyHandle OpenPolicy(uint desiredAccess)
        {
            LsaObjectAttributes attributes = new LsaObjectAttributes();
            SafeLsaPolicyHandle policy;
            int status = LsaOpenPolicy(
                IntPtr.Zero,
                ref attributes,
                desiredAccess,
                out policy
            );
            if (status != StatusSuccess)
            {
                if (policy != null) policy.Dispose();
                ThrowIfFailed(status, "LsaOpenPolicy");
            }
            if (policy == null || policy.IsInvalid)
            {
                if (policy != null) policy.Dispose();
                throw new InvalidOperationException(
                    "LsaOpenPolicy returned an invalid handle."
                );
            }
            return policy;
        }

        private static bool IsMissingAccount(int status)
        {
            if (status == StatusObjectNameNotFound) return true;
            return status != StatusSuccess &&
                LsaNtStatusToWinError(status) == ErrorFileNotFound;
        }

        private static void ValidateReturnedString(LsaUnicodeString value)
        {
            if ((value.Length & 1) != 0 || value.Length > value.MaximumLength)
                throw new InvalidDataException(
                    "LSA returned an invalid Unicode string length."
                );
            if (value.Length != 0 && value.Buffer == IntPtr.Zero)
                throw new InvalidDataException(
                    "LSA returned a null Unicode string buffer."
                );
        }

        private static void ThrowIfFailed(int status, string operation)
        {
            if (status == StatusSuccess) return;
            uint win32 = LsaNtStatusToWinError(status);
            string message = String.Format(
                System.Globalization.CultureInfo.InvariantCulture,
                "{0} failed (NTSTATUS 0x{1:X8}, Win32 {2}).",
                operation,
                unchecked((uint)status),
                win32
            );
            throw new Win32Exception(unchecked((int)win32), message);
        }
    }
}
'@

    try {
        Add-Type `
            -TypeDefinition $lsaSource `
            -Language CSharp `
            -ErrorAction Stop | Out-Null
    } catch {
        if ($null -eq ($lsaTypeName -as [type])) {
            throw
        }
    }
}

function Test-Re3DBatchLogonRight {
    param([Parameter(Mandatory = $true)][string]$AccountName)
    return [Re3D.Platform.Native.LsaBatchLogonRightV1]::Has($AccountName)
}

function Add-Re3DBatchLogonRight {
    param([Parameter(Mandatory = $true)][string]$AccountName)
    return [Re3D.Platform.Native.LsaBatchLogonRightV1]::Add($AccountName)
}

function Remove-Re3DBatchLogonRight {
    param([Parameter(Mandatory = $true)][string]$AccountName)
    return [Re3D.Platform.Native.LsaBatchLogonRightV1]::Remove($AccountName)
}

function Get-Re3DBatchLogonMarkerSettings {
    return [pscustomobject][ordered]@{
        sub_key = "SOFTWARE\Re3D\RestrictedDeployment"
        value_name = "RestrictedTaskBatchLogonRightV1"
        privilege = "SeBatchLogonRight"
        schema = 1
    }
}

function New-Re3DRestrictedMarkerRegistrySecurity {
    $security = [Security.AccessControl.RegistrySecurity]::new()
    $security.SetAccessRuleProtection($true, $false)
    $ownerSid = [Security.Principal.SecurityIdentifier]::new(
        "S-1-5-32-544"
    )
    $security.SetOwner($ownerSid)
    foreach ($sidValue in @("S-1-5-18", "S-1-5-32-544")) {
        $sid = [Security.Principal.SecurityIdentifier]::new($sidValue)
        $rule = [Security.AccessControl.RegistryAccessRule]::new(
            $sid,
            [Security.AccessControl.RegistryRights]::FullControl,
            [Security.AccessControl.InheritanceFlags]::ContainerInherit,
            [Security.AccessControl.PropagationFlags]::None,
            [Security.AccessControl.AccessControlType]::Allow
        )
        $null = $security.AddAccessRule($rule)
    }
    return $security
}

function Open-Re3DRestrictedMarkerRegistryBase {
    return [Microsoft.Win32.RegistryKey]::OpenBaseKey(
        [Microsoft.Win32.RegistryHive]::LocalMachine,
        [Microsoft.Win32.RegistryView]::Registry64
    )
}

function Assert-Re3DRestrictedMarkerRegistrySecurity {
    param([Parameter(Mandatory = $true)]$Key)

    $sections = (
        [Security.AccessControl.AccessControlSections]::Access -bor
        [Security.AccessControl.AccessControlSections]::Owner
    )
    $security = $Key.GetAccessControl(
        $sections
    )
    if (-not $security.AreAccessRulesProtected) {
        throw "Restricted deployment marker registry DACL must be protected."
    }
    $owner = $security.GetOwner(
        [Security.Principal.SecurityIdentifier]
    ).Value
    if ($owner -cne "S-1-5-32-544") {
        throw (
            "Restricted deployment marker registry owner must be the local " +
            "Administrators group."
        )
    }
    $expected = @{
        "S-1-5-18" = $false
        "S-1-5-32-544" = $false
    }
    $rules = @($security.GetAccessRules(
        $true,
        $false,
        [Security.Principal.SecurityIdentifier]
    ))
    if ($rules.Count -ne $expected.Count) {
        throw "Restricted deployment marker registry DACL contains an unexpected rule count."
    }
    foreach ($rule in $rules) {
        $sid = $rule.IdentityReference.Value
        if (-not $expected.ContainsKey($sid)) {
            throw "Restricted deployment marker registry grants an unexpected identity: $sid"
        }
        if (
            $rule.AccessControlType -ne
                [Security.AccessControl.AccessControlType]::Allow -or
            [int]$rule.RegistryRights -ne
                [int][Security.AccessControl.RegistryRights]::FullControl -or
            $rule.InheritanceFlags -ne
                [Security.AccessControl.InheritanceFlags]::ContainerInherit -or
            $rule.PropagationFlags -ne
                [Security.AccessControl.PropagationFlags]::None -or
            $rule.IsInherited
        ) {
            throw "Restricted deployment marker registry contains an unsafe access rule."
        }
        if ($expected[$sid]) {
            throw "Restricted deployment marker registry contains a duplicate identity grant."
        }
        $expected[$sid] = $true
    }
    foreach ($entry in $expected.GetEnumerator()) {
        if (-not $entry.Value) {
            throw "Restricted deployment marker registry is missing SID $($entry.Key)."
        }
    }
}

function Open-Re3DRestrictedMarkerRegistryKey {
    param([switch]$Writable, [switch]$Create)

    $settings = Get-Re3DBatchLogonMarkerSettings
    $base = Open-Re3DRestrictedMarkerRegistryBase
    try {
        $key = $base.OpenSubKey($settings.sub_key, [bool]$Writable)
        if ($null -eq $key -and $Create) {
            $registrySecurity = New-Re3DRestrictedMarkerRegistrySecurity
            $key = $base.CreateSubKey(
                $settings.sub_key,
                [Microsoft.Win32.RegistryKeyPermissionCheck]::ReadWriteSubTree,
                [Microsoft.Win32.RegistryOptions]::None,
                $registrySecurity
            )
        }
        if ($null -ne $key) {
            Assert-Re3DRestrictedMarkerRegistrySecurity -Key $key
        }
        return $key
    } finally {
        $base.Dispose()
    }
}

function Get-Re3DRestrictedBatchLogonMarker {
    param([Parameter(Mandatory = $true)][string]$ExpectedAccountSid)

    $settings = Get-Re3DBatchLogonMarkerSettings
    $key = Open-Re3DRestrictedMarkerRegistryKey
    if ($null -eq $key) {
        return $null
    }
    try {
        $raw = $key.GetValue(
            $settings.value_name,
            $null,
            [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames
        )
        if ($null -eq $raw) {
            return $null
        }
        if ($raw -isnot [string]) {
            throw "Restricted batch-logon marker must be a JSON string."
        }
        try {
            $marker = $raw | ConvertFrom-Json
        } catch {
            throw "Restricted batch-logon marker is not valid JSON."
        }
        if (
            [int]$marker.schema -ne $settings.schema -or
            [string]$marker.machine -cne $env:COMPUTERNAME -or
            [string]$marker.account_sid -cne $ExpectedAccountSid -or
            [string]$marker.privilege -cne $settings.privilege -or
            [string]$marker.state -notin @("pending-add", "owned")
        ) {
            throw "Restricted batch-logon marker does not match the fixed deployment identity."
        }
        return $marker
    } finally {
        $key.Dispose()
    }
}

function Set-Re3DRestrictedBatchLogonMarker {
    param(
        [Parameter(Mandatory = $true)][string]$AccountSid,
        [Parameter(Mandatory = $true)]
        [ValidateSet("pending-add", "owned")][string]$State
    )

    $settings = Get-Re3DBatchLogonMarkerSettings
    $key = Open-Re3DRestrictedMarkerRegistryKey -Writable -Create
    try {
        $payload = [ordered]@{
            schema = $settings.schema
            machine = $env:COMPUTERNAME
            account_sid = $AccountSid
            privilege = $settings.privilege
            state = $State
        } | ConvertTo-Json -Compress
        $key.SetValue(
            $settings.value_name,
            $payload,
            [Microsoft.Win32.RegistryValueKind]::String
        )
        $key.Flush()
    } finally {
        $key.Dispose()
    }
    $marker = Get-Re3DRestrictedBatchLogonMarker `
        -ExpectedAccountSid $AccountSid
    if ([string]$marker.state -cne $State) {
        throw "Restricted batch-logon marker did not persist state '$State'."
    }
}

function Remove-Re3DRestrictedBatchLogonMarker {
    param([Parameter(Mandatory = $true)][string]$ExpectedAccountSid)

    $settings = Get-Re3DBatchLogonMarkerSettings
    $marker = Get-Re3DRestrictedBatchLogonMarker `
        -ExpectedAccountSid $ExpectedAccountSid
    if ($null -eq $marker) {
        return
    }
    $key = Open-Re3DRestrictedMarkerRegistryKey -Writable
    try {
        $key.DeleteValue($settings.value_name, $false)
        $key.Flush()
        if ($null -ne $key.GetValue($settings.value_name, $null)) {
            throw "Restricted batch-logon marker remained after deletion."
        }
    } finally {
        $key.Dispose()
    }
}

function Assert-Re3DNoBatchLogonDenyAssignments {
    $policyPath = Join-Path $env:TEMP (
        "re3d-user-rights-$([Guid]::NewGuid().ToString('N')).cfg"
    )
    try {
        $seceditPath = Join-Path $env:SystemRoot "System32\secedit.exe"
        if (-not (Test-Path -LiteralPath $seceditPath -PathType Leaf)) {
            throw "Windows security-policy export tool was not found."
        }
        & $seceditPath `
            /export `
            /mergedpolicy `
            /cfg $policyPath `
            /areas USER_RIGHTS `
            /quiet
        if ($LASTEXITCODE -ne 0) {
            throw "Could not export effective local user-right assignments."
        }
        $deny = @(Get-Content -LiteralPath $policyPath | Where-Object {
            $_ -match '^SeDenyBatchLogonRight\s*=\s*\S+'
        })
        if ($deny.Count -ne 0) {
            throw (
                "SeDenyBatchLogonRight has assignments and may override the " +
                "required allow right. Review effective policy before provisioning."
            )
        }
    } finally {
        if (Test-Path -LiteralPath $policyPath) {
            Remove-Item -LiteralPath $policyPath -Force
        }
    }
}

function Enter-Re3DRestrictedProvisioningMutex {
    $mutex = [Threading.Mutex]::new(
        $false,
        "Global\Re3DRestrictedTaskProvisioningV1"
    )
    $acquired = $false
    try {
        try {
            $acquired = $mutex.WaitOne(0)
        } catch [Threading.AbandonedMutexException] {
            $acquired = $true
        }
        if (-not $acquired) {
            throw "Another restricted task provision or deprovision operation is running."
        }
        return $mutex
    } catch {
        if (-not $acquired) {
            $mutex.Dispose()
        }
        throw
    }
}

function Exit-Re3DRestrictedProvisioningMutex {
    param([Parameter(Mandatory = $true)][Threading.Mutex]$Mutex)
    try {
        $Mutex.ReleaseMutex()
    } finally {
        $Mutex.Dispose()
    }
}
