package org.opensearch.migrations;

import java.util.function.Predicate;

import lombok.experimental.UtilityClass;

@UtilityClass
public class VersionMatchers {
    public static final Predicate<Version> isES_2_X = VersionMatchers.matchesMajorVersion(Version.fromString("ES 2.4"));
    public static final Predicate<Version> isES_5_X = VersionMatchers.matchesMajorVersion(Version.fromString("ES 5.6"));
    public static final Predicate<Version> isES_6_X = VersionMatchers.matchesMajorVersion(Version.fromString("ES 6.8"));
<<<<<<< Updated upstream
=======
    public static final Predicate<Version> isES_6_0_to_6_1 = VersionMatchers.equalOrGreaterThanMinorVersion(Version.fromString("ES 6.0"))
                                                            .and(VersionMatchers.equalOrLessThanMinorVersion(Version.fromString("ES 6.1")));
    public static final Predicate<Version> isES_6_2_X = VersionMatchers.equalOrGreaterThanMinorVersion(Version.fromString("ES 6.2"))
                                                            .and(VersionMatchers.equalOrLessThanMinorVersion(Version.fromString("ES 6.2")));
    public static final Predicate<Version> equalOrGreaterThanES_6_5 = VersionMatchers.equalOrGreaterThanMinorVersion(Version.fromString("ES 6.5"));
>>>>>>> Stashed changes
    public static final Predicate<Version> isES_7_X = VersionMatchers.matchesMajorVersion(Version.fromString("ES 7.10"));
    public static final Predicate<Version> isES_7_10 = VersionMatchers.matchesMinorVersion(Version.fromString("ES 7.10.2"));
    public static final Predicate<Version> isES_8_X = VersionMatchers.matchesMajorVersion(Version.fromString("ES 8.17"));
    public static final Predicate<Version> isES_7_0_to_7_8 = VersionMatchers.inclusiveVersionRange("ES 7.0.0", "ES 7.8.99");
    public static final Predicate<Version> isES_7_9_X = VersionMatchers.inclusiveVersionRange("ES 7.9.0", "ES 7.9.99");
    public static final Predicate<Version> equalOrGreaterThanES_7_10 = VersionMatchers.equalOrGreaterThanMinorVersion(Version.fromString("ES 7.10"));

    public static final Predicate<Version> isOS_1_X = VersionMatchers.matchesMajorVersion(Version.fromString("OS 1.0.0"));
    public static final Predicate<Version> isOS_2_X = VersionMatchers.matchesMajorVersion(Version.fromString("OS 2.0.0"));
    public static final Predicate<Version> isOS_3_X = VersionMatchers.matchesMajorVersion(Version.fromString("OS 3.0.0"));
    public static final Predicate<Version> isOS_2_19_OrGreater = VersionMatchers.equalOrGreaterThanMinorVersion(Version.fromString("OS 2.19.0"))
                                                                    .or(VersionMatchers.isOS_3_X);
    public static final Predicate<Version> anyOS = VersionMatchers.isOS_1_X.or(VersionMatchers.isOS_2_X).or(VersionMatchers.isOS_3_X);
    public static final Predicate<Version> isES_6_0_to_6_5 = version ->
            version.getMajor() == 6 && version.getMinor() < 6;

    static Predicate<Version> matchesFlavor(final Version version) {
        return other -> {
            if (other == null) {
                return false;
            }
            if (other.getFlavor().isOpenSearch() && version.getFlavor().isOpenSearch()) {
                return true;
            }
            return other.getFlavor() == version.getFlavor();
        };
    }
    
    static Predicate<Version> matchesMajorVersion(final Version version) {
        return other -> {
            if (other == null) {
                return false;
            }
            var flavorMatches = matchesFlavor(other).test(version);
            var majorVersionNumberMatches = version.getMajor() == other.getMajor();
            return flavorMatches && majorVersionNumberMatches;
        };
    }

    static Predicate<Version> matchesMinorVersion(final Version version) {
        return other -> matchesMajorVersion(version)
            .and(other2 -> version.getMinor() == other2.getMinor())
            .test(other);
    }

    static int compareVersions(Version v1, Version v2) {
        if (v1.getMajor() != v2.getMajor()) {
            return Integer.compare(v1.getMajor(), v2.getMajor());
        }
        if (v1.getMinor() != v2.getMinor()) {
            return Integer.compare(v1.getMinor(), v2.getMinor());
        }
        return Integer.compare(v1.getPatch(), v2.getPatch());
    }

    static Predicate<Version> inclusiveVersionRange(String minVersionInclusiveStr, String maxVersionInclusiveStr) {
        Version minVersion = Version.fromString(minVersionInclusiveStr);
        Version maxVersion = Version.fromString(maxVersionInclusiveStr);
        return v -> {
            if (v == null) {
                return false;
            }
            int compareToMin = compareVersions(v, minVersion);
            int compareToMax = compareVersions(v, maxVersion);
            return compareToMin >= 0 && compareToMax <= 0;
        };
    }

    static Predicate<Version> equalOrGreaterThanMinorVersion(final Version version) {
        return other -> matchesMajorVersion(version)
            .and(other2 -> version.getMinor() <= other2.getMinor())
            .test(other);
    }
}
