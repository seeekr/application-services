#
# This script can be used to generate a summary of our third-party dependencies,
# including license details. Use it like this:
#
#    $> python3 dependency_summary.py --package <package name>
#
# It shells out to `cargo metadata` to gather information about the full dependency tree
# and to `cargo build --build-plan` to figure out the dependencies of the specific target package.
#
# N.B. to generate dependencies for iOS build targets, you have to run this on a Mac,
# otherwise the necessary targets are simply not available in cargo. This is a very sad state
# of affairs but I haven't been able to find a way around it.
#
# XXX TODO: include optional notice for SQLite and zlib (via adler32)
# XXX TODO: Apache license makes special mention of handling of a "NOTICE" text file included
#           in the distribution, we should check for this explicitly.

import io
import re
import sys
import os.path
import argparse
import subprocess
import hashlib
import json
import itertools
import collections
import requests

# The targets used by rust-android-gradle, excluding the ones for unit testing.
# https://github.com/mozilla/rust-android-gradle/blob/master/plugin/src/main/kotlin/com/nishtahir/RustAndroidPlugin.kt
ALL_ANDROID_TARGETS = [
    "armv7-linux-androideabi",
    "aarch64-linux-android",
    "i686-linux-android",
    "x86_64-linux-android",
]

# The targets used when compiling for iOS.
# From ../build-scripts/xc-universal-binary.sh
ALL_IOS_TARGETS = [
    "x86_64-apple-ios",
    "aarch64-apple-ios"
]

# All the targets that we build for, including unittest bundles.
# https://github.com/mozilla/rust-android-gradle/blob/master/plugin/src/main/kotlin/com/nishtahir/RustAndroidPlugin.kt
ALL_TARGETS = ALL_ANDROID_TARGETS + ALL_IOS_TARGETS + [
    "x86_64-unknown-linux-gnu",
    "x86_64-apple-darwin",
    "x86_64-pc-windows-msvc",
    "x86_64-pc-windows-gnu",
]

# The licenses under which we can compatibly use dependencies,
# in the order in which we prefer them.
LICENES_IN_PREFERENCE_ORDER = [
    # MPL is our own license and is therefore clearly the best :-)
    "MPL-2.0",
    # We like Apache2.0 because of its patent grant clauses, and its
    # easily-dedupable license text that doesn't get customized per project.
    "Apache-2.0",
    # The MIT license is pretty good, because it's short.
    "MIT",
    # Creative Commons Zero is the only Creative Commons license that's MPL-comaptible.
    # It's the closest thing around to a "public domain" license and is recommended
    # by Mozilla for use on e.g. testing code.
    "CC0-1.0",
    # BSD and similar licenses are pretty good; the fewer clauses the better.
    "ISC",
    "BSD-2-Clause",
    "BSD-3-Clause",
]


# Packages that get pulled into our dependency tree but we know we definitely don't
# ever build with in practice, typically because they're platform-specific support
# for platforms we don't actually support.
EXCLUDED_PACKAGES = set([
    "cloudabi",
    "fuchsia-cprng",
    "fuchsia-zircon",
    "fuchsia-zircon-sys",
])

# Known metadata for special extra packages that are not managed by cargo.
EXTRA_PACKAGE_METADATA = {
    "ext-jna" : {
        "name": "jna",
        "repository": "https://github.com/java-native-access/jna",
        "license": "Apache-2.0",
        "license_file": "https://raw.githubusercontent.com/java-native-access/jna/master/AL2.0",
    },
    "ext-protobuf": {
        "name": "protobuf",
        "repository": "https://github.com/protocolbuffers/protobuf",
        "license": "BSD-3-Clause",
        "license_file": "https://raw.githubusercontent.com/protocolbuffers/protobuf/master/LICENSE",
    },
    "ext-swift-protobuf": {
        "name": "swift-protobuf",
        "repository": "https://github.com/apple/swift-protobuf",
        "license": "Apache-2.0",
        "license_file": "https://raw.githubusercontent.com/apple/swift-protobuf/master/LICENSE.txt"
    },
    "ext-openssl": {
        "name": "openssl",
        "repository": "https://www.openssl.org/source/",
        "license": "OpenSSL",
        "license_file": "https://www.openssl.org/source/license-openssl-ssleay.txt",
    },
    "ext-sqlcipher": {
        "name": "sqlcipher",
        "repository": "https://github.com/sqlcipher/sqlcipher",
        "license": "BSD-3-Clause",
        "license_file": "https://raw.githubusercontent.com/sqlcipher/sqlcipher/master/LICENSE",
    },
}

# And these are rust packages that pull in the above dependencies.
# Others are added on a per-target basis during dependency resolution.
PACKAGES_WITH_EXTRA_DEPENDENCIES = {
    "openssl-sys": ["ext-openssl"],
    "ring": ["ext-openssl"],
    # As a special case, we know that the "logins" crate is the only thing that enables SQLCipher.
    # In a future iteration we could check the cargo build-plan output to see whether anything is
    # enabling the sqlcipher feature, but this will do for now.
    "logins": ["ext-sqlcipher"],
}

# Hand-audited tweaks to package metadata, for cases where the data given to us by cargo is insufficient.
# Let's try not to add any more dependencies that require us to edit this list!
#
# For each field we want to tweak, we list both the expected value from `cargo metadata` and the replacement
# value we want to apply, like this:
#
#  {
#    "example-package": {
#      "license": {         # <-- the field we want to tweak
#         "check": None     # <-- the value from `cargo metadata` (in this case, check that it's empty)
#         "fixup": "MIT"    # <-- the value we want to replace it with
#      }
#    }
#  }
#
# This is designed to prevent us from accidentally overriting future upstream changes in package metadata.
PACKAGE_METADATA_FIXUPS = {
    # Ring's license describes itself as "ISC-like", and we've reviewed this manually.
    "ring": {
        "license": {
            "check": None,
            "fixup": "ISC",
        },
    },
    # In this case the rust code is BSD-3-Clause and the wrapped zlib library is under the Zlib license,
    # which does not require explicit attribution.
    "adler32": {
        "license": {
            "check": "BSD-3-Clause AND Zlib",
            "fixup": "BSD-3-Clause",
        },
        "license_file": {
            "check": None,
            "fixup": "LICENSE",
        }
    },
    # These packages do not unambiguously delcare their licensing file.
    "publicsuffix": {
        "license": {
            "check": "MIT/Apache-2.0"
        },
        "license_file": {
            "check": None,
            "fixup": "LICENSE-APACHE",
        }
    },
    "siphasher": {
        "license": {
            "check": "MIT/Apache-2.0"
        },
        "license_file": {
            "check": None,
            "fixup": "COPYING",
        }
    },
    # These packages do not include their license file in their release distributions,
    # so we have to fetch it over the network. Each has been manually checked and resolved
    # to a final URL from which the file can be fetched (typically based on the *name* of
    # the license file as declared in cargo metadata).
    # XXX TODO: File upstream bugs to get it included in the release distribution?
    "failure_derive": {
        "repository": {
            "check": "https://github.com/withoutboats/failure_derive",
        },
        "license_file": {
            "check": None,
            "fixup": "https://raw.githubusercontent.com/withoutboats/failure_derive/master/LICENSE-APACHE",
        }
    },
    "hawk": {
        "repository": {
            "check": "https://github.com/taskcluster/rust-hawk",
        },
        "license_file": {
            "check": None,
            "fixup": "https://raw.githubusercontent.com/taskcluster/rust-hawk/master/LICENSE",
        }
    },
    "kernel32-sys": {
        "repository": {
            "check": "https://github.com/retep998/winapi-rs",
        },
        "license_file": {
            "check": None,
            "fixup": "https://raw.githubusercontent.com/retep998/winapi-rs/master/LICENSE-APACHE",
        }
    },
    "libsqlite3-sys": {
        "repository": {
            "check": "https://github.com/jgallagher/rusqlite",
        },
        "license_file": {
            "check": None,
            "fixup": "https://raw.githubusercontent.com/jgallagher/rusqlite/master/LICENSE",
        }
    },
    "phf": {
        "repository": {
            "check": "https://github.com/sfackler/rust-phf",
        },
        "license_file": {
            "check": None,
            "fixup": "https://raw.githubusercontent.com/sfackler/rust-phf/master/LICENSE",
        }
    },
    "phf_codegen": {
        "repository": {
            "check": "https://github.com/sfackler/rust-phf",
        },
        "license_file": {
            "check": None,
            "fixup": "https://raw.githubusercontent.com/sfackler/rust-phf/master/LICENSE",
        }
    },
    "phf_generator": {
        "repository": {
            "check": "https://github.com/sfackler/rust-phf",
        },
        "license_file": {
            "check": None,
            "fixup": "https://raw.githubusercontent.com/sfackler/rust-phf/master/LICENSE",
        },
    },
    "phf_shared": {
        "repository": {
            "check": "https://github.com/sfackler/rust-phf",
        },
        "license_file": {
            "check": None,
            "fixup": "https://raw.githubusercontent.com/sfackler/rust-phf/master/LICENSE",
        },
    },
    "prost-build": {
        "repository": {
            "check": "https://github.com/danburkert/prost",
        },
        "license_file": {
            "check": None,
            "fixup": "https://raw.githubusercontent.com/danburkert/prost/master/LICENSE",
        },
    },
    "prost-derive": {
        "repository": {
            "check": "https://github.com/danburkert/prost",
        },
        "license_file": {
            "check": None,
            "fixup": "https://raw.githubusercontent.com/danburkert/prost/master/LICENSE",
        },
    },
    "prost-types": {
        "repository": {
            "check": "https://github.com/danburkert/prost",
        },
        "license_file": {
            "check": None,
            "fixup": "https://raw.githubusercontent.com/danburkert/prost/master/LICENSE",
        },
    },
    "security-framework": {
        "repository": {
            "check": "https://github.com/kornelski/rust-security-framework",
        },
        "license_file": {
            "check": None,
            "fixup": "https://raw.githubusercontent.com/kornelski/rust-security-framework/master/LICENSE-APACHE",
        },
    },
    "security-framework-sys": {
        "repository": {
            "check": "https://github.com/kornelski/rust-security-framework",
        },
        "license_file": {
            "check": None,
            "fixup": "https://raw.githubusercontent.com/kornelski/rust-security-framework/master/LICENSE-APACHE",
        },
    },
    "url_serde": {
        "repository": {
            "check": "https://github.com/servo/rust-url",
        },
        "license_file": {
            "check": None,
            "fixup": "https://raw.githubusercontent.com/servo/rust-url/master/LICENSE-APACHE",
        },
    },
    "winapi-build": {
        "repository": {
            "check": "https://github.com/retep998/winapi-rs",
        },
        "license_file": {
            "check": None,
            "fixup": "https://raw.githubusercontent.com/retep998/winapi-rs/master/LICENSE-APACHE",
        },
    },
    "winapi-x86_64-pc-windows-gnu": {
        "repository": {
            "check": "https://github.com/retep998/winapi-rs",
        },
        "license_file": {
            "check": None,
            "fixup": "https://raw.githubusercontent.com/retep998/winapi-rs/master/LICENSE-APACHE",
        },
    },
    "ws2_32-sys": {
        "repository": {
            "check": "https://github.com/retep998/winapi-rs",
        },
        "license_file": {
            "check": None,
            "fixup": "https://raw.githubusercontent.com/retep998/winapi-rs/master/LICENSE-APACHE",
        },
    }
}

# Sets of common licence file names, by license type.
# If we can find one and only one of these files in a package, then we can be confident
# that it's the intended license text.
COMMON_LICENSE_FILE_NAME_ROOTS = {
    "": ["license", "licence"],
    "Apache-2.0": ["license-apache", "licence-apache"],
    "MIT": ["license-mit", "licence-mit"],
}
COMMON_LICENSE_FILE_NAME_SUFFIXES = ["", ".md", ".txt"]
COMMON_LICENSE_FILE_NAMES = {}
for license in COMMON_LICENSE_FILE_NAME_ROOTS:
    COMMON_LICENSE_FILE_NAMES[license] = set()
    for suffix in COMMON_LICENSE_FILE_NAME_SUFFIXES:
        for root in COMMON_LICENSE_FILE_NAME_ROOTS[license]:
            COMMON_LICENSE_FILE_NAMES[license].add(root + suffix)
        for root in COMMON_LICENSE_FILE_NAME_ROOTS[""]:
            COMMON_LICENSE_FILE_NAMES[license].add(root + suffix)


def get_workspace_metadata():
    """Get metadata for all dependencies in the workspace."""
    p = subprocess.run([
        'cargo', '+nightly', 'metadata', '--locked', '--format-version', '1'
    ], stdout=subprocess.PIPE, universal_newlines=True)
    p.check_returncode()
    return WorkspaceMetadata(json.loads(p.stdout))


def print_dependency_summary(deps, file=sys.stdout):
    """Print a nicely-formatted summary of dependencies and their license info."""
    def pf(string, *args):
        if args:
            string = string.format(*args)
        print(string, file=file)

    # Dedupe by shared license text where possible.
    depsByLicenseTextHash = collections.defaultdict(list)
    for info in deps:
        if info["license"] in ("MPL-2.0", "Apache-2.0", "OpenSSL"):
            # We know these licenses to have shared license text, sometimes differing on e.g. punctuation details.
            # XXX TODO: should check this more explicitly to ensure they contain the expected text.
            licenseTextHash = info["license"]
        else:
            # Other license texts typically include copyright notices that we can't dedupe, except on whitespace.
            text = "".join(info["license_text"].split())
            licenseTextHash = info["license"] + ":" + hashlib.sha256(text.encode("utf8")).hexdigest()
        depsByLicenseTextHash[licenseTextHash].append(info)

    # List licenses in the order in which we prefer them, then in alphabetical order
    # of the dependency names. This ensures a convenient and stable ordering.
    def sort_key(licenseTextHash):
        for i, license in enumerate(LICENES_IN_PREFERENCE_ORDER):
            if licenseTextHash.startswith(license):
                return (i, sorted(info["name"] for info in depsByLicenseTextHash[licenseTextHash]))
        return (i + 1, sorted(info["name"] for info in depsByLicenseTextHash[licenseTextHash]))

    sections = sorted(depsByLicenseTextHash.keys(), key=sort_key)

    pf("# Licenses for Third-Party Dependencies")
    pf("")
    pf("Software packages built from this source code may incorporate code from a number of third-party dependencies.")
    pf("These dependencies are available under a variety of free and open source licenses,")
    pf("the details of which are reproduced below.")
    pf("")

    # First a "table of contents" style thing.
    for licenseTextHash in sections:
        header = format_license_header(licenseTextHash, depsByLicenseTextHash[licenseTextHash])
        pf("* [{}](#{})", header, header_to_anchor(header))

    pf("-------------")

    # Now the actual license details.
    for licenseTextHash in sections:
        deps = sorted(depsByLicenseTextHash[licenseTextHash], key=lambda i: i["name"])
        licenseText = deps[0]["license_text"]
        for dep in deps:
            licenseText = dep["license_text"]
            # As a bit of a hack, we need to find a copy of the apache license text
            # that still has the copyright placeholders in it.
            if licenseTextHash != "Apache-2.0" or "[yyyy]" in licenseText:
                break
        else:
            raise RuntimeError("Could not find appropriate apache license text")
        pf("## {}", format_license_header(licenseTextHash, deps))
        pf("")
        pkgs = ["[{}]({})".format(info["name"], info["repository"]) for info in deps]
        pkgs = sorted(set(pkgs)) # Dedupe in case of multiple versons of dependencies.
        pf("This license applies to code linked from the following dependendencies: {}", ", ".join(pkgs))
        pf("")
        pf("```")
        assert "```" not in licenseText
        pf("{}", licenseText)
        pf("```")
        pf("-------------")


def format_license_header(license, deps):
    if license == "MPL-2.0":
        return "Mozilla Public License 2.0"
    if license == "Apache-2.0":
        return "Apache License 2.0"
    if license == "OpenSSL":
        return "OpenSSL License"
    license = license.split(":")[0]
    # Dedupe in case of multiple versons of dependencies
    names=sorted(set(info["name"] for info in deps))
    return "{} License: {}".format(license, ", ".join(names))


def header_to_anchor(header):
    return header.lower().replace(" ", "-").replace(".", "").replace(",", "").replace(":", "")


class WorkspaceMetadata(object):
    """Package metadata for all dependencies in the workspace.

    This uses `cargo metadata` to load the complete set of package metadata for the dependency tree
    of our workspace.  This typically lists too many packages, because it does a union of all features
    required by all packages in the workspace. Use the `get_package_dependencies` to obtain the
    set of depdencies for a specific package, based on its build plan.
    
    For the JSON data format, ref https://doc.rust-lang.org/cargo/commands/cargo-metadata.html
    """

    def __init__(self, metadata):
        self.metadata = metadata
        self.pkgInfoById = {}
        self.pkgInfoByManifestPath = {}
        self.workspaceMembersByName = {}
        for info in metadata["packages"]:
            if info["name"] in EXCLUDED_PACKAGES:
                continue
            # Apply any hand-rolled fixups, carefully checking that they haven't been invalidated.
            if info["name"] in PACKAGE_METADATA_FIXUPS:
                fixups = PACKAGE_METADATA_FIXUPS[info["name"]]
                for key, change in fixups.items():
                    if info.get(key, None) != change["check"]:
                        assert False, "Fixup check failed for {}.{}: {} != {}".format(
                            info["name"], key,  info.get(key, None), change["check"])
                    if "fixup" in change:
                        info[key] = change["fixup"]
            # Index packages for fast lookup.
            assert info["id"] not in self.pkgInfoById
            self.pkgInfoById[info["id"]] = info
            assert info["manifest_path"] not in self.pkgInfoByManifestPath
            self.pkgInfoByManifestPath[info["manifest_path"]] = info
        # Add fake packages for things managed outside of cargo.
        for name, info in EXTRA_PACKAGE_METADATA.items():
            assert name not in self.pkgInfoById
            self.pkgInfoById[name] = info.copy()
        for id in metadata["workspace_members"]:
            name = self.pkgInfoById[id]["name"]
            assert name not in self.workspaceMembersByName
            self.workspaceMembersByName[name] = id

    def has_package(self, id):
        return id in self.pkgInfoById

    def get_package_by_id(self, id):
        return self.pkgInfoById[id]

    def get_package_by_manifest_path(self, path):
        return self.pkgInfoByManifestPath[path]

    def get_dependency_summary(self, name=None, targets=None):
        """Get dependency and license summary infomation.

        Called with no arguments, this method will yield dependency summary information for all packages
        in the workspace.  When the `name` argument is specified it will yield information for just
        the dependencies of that package.  When the `targets` argument is specified it will yield
        information for the named package when compiled for just those targets.  Thus, each argument
        will produce a narrower set of dependencies.
        """
        if name is not None:
            deps = self.get_package_dependencies(name, targets)
        else:
            deps = set()
            # Deliberately not using `cargo build --all` for this in order to avoid possible
            # interaction between cargo features enabled by different projects.
            # XXX TODO: this takes a long time, and I'm not sure it's that valuable.
            # Might go away once we have full-megazord to depend on.
            for nm in self.workspaceMembersByName:
                deps |= self.get_package_dependencies(nm, targets)
            # As a bit of a hack, use this opportunity to check that we're not carrying around
            # outdated metadata fixups for things that aren't dependencies.
            allDepNames = set(self.pkgInfoById[dep]["name"] for dep in deps)
            unnecessaryDeps = [
                dep for dep in PACKAGE_METADATA_FIXUPS if dep not in allDepNames]
            if unnecessaryDeps:
                raise RuntimeError(
                    "Unnecessary dependencies in PACKAGE_METADATA_FIXUPS: '{}'".format(unnecessaryDeps))
        for id in deps:
            if not self.is_external_dependency(id):
                continue
            yield self.get_license_info(id)

    def get_package_dependencies(self, name, targets=None):
        """Get the set of dependencies for the named package, when compiling for the specified targets.
        
        This implementation uses `cargo build --build-plan` to list all inputs to the build process.
        It has the advantage of being guaranteed to correspond to what's included in the actual build,
        but requires using unstable cargo features.

        If a package name is not provided, then all packages in the workspace are examined.
        """
        targets = self.get_compatible_targets_for_package(name, targets)
        cmd = (
            'cargo', '+nightly', '-Z', 'unstable-options', 'build',
            '--build-plan',
            '--quiet',
            '--locked',
            '--package', name,
        )
        deps = set()
        for target in targets:
            args = ('--target', target,)
            p = subprocess.run(cmd + args, stdout=subprocess.PIPE, universal_newlines=True)
            p.check_returncode()
            buildPlan = json.loads(p.stdout)
            for manifestPath in buildPlan['inputs']:
                info = self.get_package_by_manifest_path(manifestPath)
                deps.add(info['id'])
        deps |= self.get_extra_dependencies_not_managed_by_cargo(name, targets, deps)
        return deps

    def get_extra_dependencies_not_managed_by_cargo(self, name, targets, deps):
        """Get additional dependencies for things managed outside of cargo.

        This includes optional C libraries like SQLCipher, as well as platform-specific
        dependencies for our various language bindings.
        """
        extras = set()
        for target in targets:
            if self.target_is_android(target):
                extras.add("ext-jna")
                extras.add("ext-protobuf")
            if self.target_is_ios(target):
                extras.add("ext-swift-protobuf")
        for dep in deps:
            name = self.pkgInfoById[dep]["name"]
            if name in PACKAGES_WITH_EXTRA_DEPENDENCIES:
                extras |= set(PACKAGES_WITH_EXTRA_DEPENDENCIES[name])
        return extras

    def get_compatible_targets_for_package(self, name, targets=None):
        """Get the set of targets that are compatible with the named package.

        Some targets (e.g. iOS) cannot build certains types of package (e.g. cdylib)
        so we use this method to filter the set of targets back on package type.
        """
        if not targets:
            targets = ALL_TARGETS
        elif isinstance(targets, str):
            targets = (targets,)
        pkgInfo = self.pkgInfoById[self.workspaceMembersByName[name]]
        # Can't build cdylibs on iOS targets.
        for buildTarget in pkgInfo["targets"]:
            if "cdylib" in buildTarget["kind"]:
                targets = [target for target in targets if not self.target_is_ios(target)]
        return targets

    def target_is_android(self, target):
        """Determine whether the given build target is for an android platform."""
        if target.endswith("-android") or target.endswith("-androideabi"):
            return True
        return False

    def target_is_ios(self, target):
        """Determine whether the given build target is for an iOS platform."""
        if target.endswith("-ios"):
            return True
        return False

    def is_external_dependency(self, id):
        """Check whether the named package is an external dependency."""
        pkgInfo = self.pkgInfoById[id]
        try:
            if pkgInfo["source"] is not None:
                return True
        except KeyError:
            # There's no "source" key in info for externally-managed dependencies
            return True
        manifest = pkgInfo["manifest_path"]
        root = os.path.commonprefix([manifest, self.metadata["workspace_root"]])
        if root != self.metadata["workspace_root"]:
            return True
        return False

    def get_manifest_path(self, id):
        """Get the path to a package's Cargo manifest."""
        return self.pkgInfoById[id]["manifest_path"]

    def get_license_info(self, id):
        """Get the licensing info for the named dependency, or error if it can't be detemined."""
        pkgInfo = self.pkgInfoById[id]
        chosenLicense = self.pick_most_acceptable_license(id, pkgInfo["license"])
        return {
            "name": pkgInfo["name"],
            "repository": pkgInfo["repository"],
            "license": chosenLicense,
            "license_text": self._fetch_license_text(id, chosenLicense, pkgInfo),
        }

    def pick_most_acceptable_license(self, id, licenseId):
        """Select the best license under which to redistribute a dependency.

        This parses the SPDX-style license identifiers included in our dependencies
        and selects the best license for our needs, where "best" is a subjective judgement
        based on whether it's acceptable at all, and then how convenient it is to work with
        here in the license summary tool...
        """
        # Split "A/B" and "A OR B" into individual license names.
        licenses = set(l.strip() for l in re.split(r"\s*(?:/|\sOR\s)\s*", licenseId))
        # Try to pick the "best" compatible license available.
        for license in LICENES_IN_PREFERENCE_ORDER:
            if license in licenses:
                return license
        # OK now we're into the special snowflakes, and we want to be careful
        # not to unexpectedly accept new dependencies under these licenes.
        if "OpenSSL" in licenses:
            if id == "ext-openssl":
                return "OpenSSL"
        raise RuntimeError("Could not determine acceptable license for {}; license is '{}'".format(id, licenseId))

    def _fetch_license_text(self, id, license, pkgInfo):
        if "license_text" in pkgInfo:
            return pkgInfo["license_text"]
        licenseFile = pkgInfo.get("license_file", None)
        if licenseFile is not None:
            if licenseFile.startswith("https://"):
                r = requests.get(licenseFile)
                r.raise_for_status()
                return r.content.decode("utf8")
            else:
                pkgRoot = os.path.dirname(pkgInfo["manifest_path"])
                with open(os.path.join(pkgRoot, licenseFile)) as f:
                    return f.read()
        # No explicit license file was declared, let's see if we can unambiguously identify one
        # using common naming conventions.
        pkgRoot = os.path.dirname(pkgInfo["manifest_path"])
        try:
            licenseFileNames = COMMON_LICENSE_FILE_NAMES[license]
        except KeyError:
            licenseFileNames = COMMON_LICENSE_FILE_NAMES[""]
        foundLicenseFiles = [nm for nm in os.listdir(pkgRoot) if nm.lower() in licenseFileNames]
        if len(foundLicenseFiles) == 1:
            with open(os.path.join(pkgRoot, foundLicenseFiles[0])) as f:
                return f.read()
        # We couldn't find the right license text. Let's do what we can to help a human
        # pick the right one and add it to the list of manual fixups.
        if len(foundLicenseFiles) > 1:
            err = "Multiple ambiguous license files found for '{}'.\n".format(pkgInfo["name"])
            err += "Please select the correct license file and add it to `PACKAGE_METADATA_FIXUPS`.\n"
            err += "Potential license files: {}".format(foundLicenseFiles)
        else:
            err = "Could not find license file for '{}'.\n".format(pkgInfo["name"])
            err += "Please locate the correct license file and add it to `PACKAGE_METADATA_FIXUPS`.\n"
            err += "You may need to poke around in the source repository at {}".format(pkgInfo["repository"])
        raise RuntimeError(err)
    

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="summarize dependencies and license information")
    parser.add_argument('-p', '--package', action="store")
    parser.add_argument('--target', action="append", dest="targets")
    parser.add_argument('--all-android-targets', action="append_const", dest="targets", const=ALL_ANDROID_TARGETS)
    parser.add_argument('--all-ios-targets', action="append_const", dest="targets", const=ALL_IOS_TARGETS)
    parser.add_argument('--json', action="store_true", help="output JSON rather than human-readable text")
    parser.add_argument('--check', action="store", help="suppress output, instead checking that it matches the given file")
    args = parser.parse_args()
    if args.targets:
        if args.package is None:
            raise RuntimeError("You must specify a package name when specifying targets")
        # Flatten the lists introduced by --all-XXX-targets options.
        args.targets = list(itertools.chain(*([t] if isinstance(t, str) else t for t in args.targets)))

    metadata = get_workspace_metadata()
    deps = metadata.get_dependency_summary(args.package, args.targets)

    if args.check:
        output = io.StringIO()
    else:
        output = sys.stdout

    if args.json:
        json.dump([info for info in deps], output)
    else:
        print_dependency_summary(deps, file=output)

    if args.check:
        with open(args.check, 'r') as f:
            if f.read() != output.getvalue():
                raise RuntimeError("Dependency details have changed from those in {}".format(args.check))

